"""microweb - a tiny Flask-style web framework. Standard library only.

Flask-style, not Flask-compatible: just enough for a small local app.

    from microweb import App, HTTPError

    app = App(static_dir="static")

    @app.get("/api/hello")
    def hello(req):
        name = req.query.get("name", "world")
        return {"message": f"hello {name}"}

    @app.post("/api/echo")
    def echo(req):
        data = req.json()
        if not isinstance(data, dict):
            raise HTTPError(400, "expected a JSON object")
        return data, 201

    app.run("127.0.0.1", 8080, on_ready=lambda: print("listening"))

Handlers take one argument, the Request (req), and return:
    dict or list      -> JSON, status 200
    str               -> text/plain, status 200
    bytes             -> application/octet-stream, status 200
    None              -> 204 No Content
    Stream(chunks, content_type)
                      -> the chunks sent one by one as they're produced
    Response(body, status, content_type, headers)
                      -> explicit response metadata around any supported body
    (body, status)    -> any of the above with that status
raise HTTPError(status, message, **extra) -> JSON {"error": message, **extra}.
Any other exception -> 500 {"error": "internal error"}, traceback logged.

Lookup order: exact-path route; mounted app; for GET/HEAD, a file from static_dir ("/" serves
index.html; no directory listings); else 404. A known path with the wrong
method gets 405 with an Allow header. Request bodies need a Content-Length
(411 otherwise) and are limited to max_body bytes (1 MB by default, raised per
call with req.body(max_bytes=...)).

Each request runs in its own thread, and each connection serves one request
(HTTP/1.0 style). Logging goes to the "microweb" logger: unexpected exceptions
as errors with a traceback; request lines and HTTPErrors at debug level (a
handler that raises HTTPError logs what's worth logging itself).

App.mount(prefix, child) composes independent apps; req.url(path) builds links
using their mounting prefix. Shell is an optional landing page and navigation
around those apps. No shell, update checks, or server starts on import.
"""

import argparse
import ast
import base64
import copy
from collections import deque
import errno
import hashlib
import html
import http.client
import io
import json
import logging
import os
import re
import select
import socket
import struct
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "v0.2.3"
UPDATE_URL = "https://raw.githubusercontent.com/gilbertgong/microweb/main/microweb.version"

# Copyright (c) 2026, Gilbert Gong
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

log = logging.getLogger("microweb")

# Explicit content types: Windows' registry can map .js to text/plain, which
# breaks JavaScript modules.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".woff2": "font/woff2",
}


class HTTPError(Exception):
    """Raise in a handler to send an error response: JSON {"error": message, **extra}."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class Stream:
    """Return from a handler to send the body in pieces as they're produced.

    chunks: an iterable (e.g. a generator) of bytes or str. Each piece is sent
    as soon as it's produced; the response ends when the iterable does (the
    connection closes, so no Content-Length is needed).
    """

    def __init__(self, chunks, content_type="application/octet-stream"):
        self.chunks = chunks
        self.content_type = content_type

    def __iter__(self):
        for chunk in self.chunks:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk


class Request:
    """An incoming request.

    method   "GET", "POST", ...
    path     the URL path, without the query string
    query    dict of query-string values (the last one wins if repeated)
    headers  case-insensitive mapping
    client   the client's IP address
    body()   the raw body (bytes); json() parses it
    """

    def __init__(self, method, target, headers, rfile, client, max_body):
        url = urllib.parse.urlsplit(target)
        self.method = method
        self.path = urllib.parse.unquote(url.path)
        self.query_string = url.query
        self.root_path = ""
        self.query = dict(urllib.parse.parse_qsl(url.query))
        self.headers = headers
        self.client = client
        self._rfile = rfile
        self._max_body = max_body
        self._body = None

    def url(self, path="/"):
        """A URL within this app, including its mounting prefix."""
        return self.root_path + "/" + path.lstrip("/")

    def body(self, max_bytes=None):
        """The raw request body, read once (later calls return the same bytes).

        Raises HTTPError 411 without a Content-Length, 413 if it's larger than
        max_bytes (default: the app's max_body), 400 if it's malformed.
        """
        if self._body is not None:
            return self._body
        limit = self._max_body if max_bytes is None else max_bytes
        if "Transfer-Encoding" in self.headers:
            raise HTTPError(411, "chunked request bodies are not supported; send a Content-Length")
        length = self.headers.get("Content-Length")
        if length is None:
            if self.method in ("GET", "HEAD"):
                self._body = b""
                return self._body
            raise HTTPError(411, "Content-Length required")
        try:
            length = int(length)
            if length < 0:
                raise ValueError
        except ValueError:
            raise HTTPError(400, "invalid Content-Length") from None
        if length > limit:
            raise HTTPError(413, f"request body too large ({length} bytes; limit {limit})")
        self._body = self._rfile.read(length)
        if len(self._body) < length:
            raise HTTPError(400, "request body incomplete")
        return self._body

    def json(self, max_bytes=None):
        """The body parsed as JSON (any JSON value). Raises HTTPError 400 if it isn't valid JSON."""
        try:
            return json.loads(self.body(max_bytes))
        except ValueError:
            raise HTTPError(400, "invalid JSON") from None


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # don't wait for open requests on shutdown
    # On Windows, SO_REUSEADDR lets a second server bind a port that's already
    # in use, so only use it elsewhere (where it allows quick restarts).
    allow_reuse_address = os.name != "nt"

    def __init__(self, *args, **kwargs):
        self._requests = threading.Condition()
        self._active = 0
        self._pending = set()
        self._websockets = set()
        self._stopping = False
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # Count before starting the thread, including requests not yet dispatched.
        request.settimeout(30)
        with self._requests:
            self._active += 1
            self._pending.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._requests:
                self._pending.discard(request)
                self._active -= 1
                self._requests.notify_all()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._requests:
                self._pending.discard(request)
                self._active -= 1
                self._requests.notify_all()

    def drain_requests(self):
        with self._requests:
            self._stopping = True
            pending = list(self._pending)
        # Wake handlers waiting for a request line or headers. Requests racing
        # with shutdown may be disconnected; dispatched handlers still drain.
        for connection in pending:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        # Windows buffered readers may outlive socket shutdown; they cannot
        # dispatch once stopping, so wait only for already-dispatched handlers.
        with self._requests:
            while self._active > len(self._pending):
                self._requests.wait(0.2)  # a second Ctrl+C interrupts the drain

    def stop_websockets(self):
        with self._requests:
            self._stopping = True
            connections = list(self._websockets)
        for connection in connections:
            connection.close(1001, "server stopping", timeout=0.2)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# Optional WebSocket transport -----------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_MAX_PARTS = 256


class _WebSocketError(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason


def _allowed_origin(origin, host):
    """Match HTTPS hosts, with HTTP exceptions for explicit loopback names."""
    try:
        parsed = urllib.parse.urlsplit("//" + host)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return False
        parsed.port  # reject malformed ports as well as malformed IPv6 brackets
    except ValueError:
        return False
    return (origin == "https://" + host or
            (parsed.hostname in {"localhost", "127.0.0.1", "::1"} and origin == "http://" + host))


def _ws_close_code(code):
    return type(code) is int and (code in (1000, 1001, 1002, 1003, 1007, 1008,
                                           1009, 1010, 1011, 1012, 1013, 1014)
                                 or 3000 <= code <= 4999)


def _ws_unmask(payload, mask):
    """Apply the WebSocket frame mask using bulk integer XOR."""
    size = len(payload)
    repeated = (mask * ((size + 3) // 4))[:size]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(repeated, "little")).to_bytes(size, "little")


class WebSocket:
    """One upgraded connection. receive() returns str/bytes, or None after closure.

    Applications normally use this through @app.websocket, not its constructor.
    Sends may come from other threads; only one receiver is allowed at a time.
    No compression/extensions, automatic reconnect, or application broadcast queue.
    """

    def __init__(self, sock, req, *, initial=b"", max_bytes=1048576, timeout=10,
                 ping_interval=20, ping_timeout=20, max_queue=256, max_queue_bytes=None):
        self.req, self.max_bytes, self.timeout = req, max_bytes, timeout
        self._ping_interval, self._ping_timeout, self._max_queue = ping_interval, ping_timeout, max_queue
        self._max_queue_bytes = 4 * max_bytes if max_queue_bytes is None else max_queue_bytes
        self._queued_bytes, self._message_bytes = 0, 0
        self._next_ping = time.monotonic() + ping_interval if ping_interval is not None else None
        self._pending_ping = None
        self._messages, self._incoming = deque(), threading.Condition()
        self._sock, self._buffer = sock, bytearray(initial)
        self._send_lock, self._receive_lock = threading.Lock(), threading.Lock()
        self._closed = threading.Event()
        self._close_sent = False
        self._close_deadline = None
        self._fragment_opcode, self._fragment = None, bytearray()
        self.close_code, self.close_reason = None, ""
        sock.setblocking(False)
        self._reader_thread = threading.Thread(target=self._read_loop, name="microweb websocket", daemon=True)
        self._reader_thread.start()

    @property
    def closed(self):
        return self._closed.is_set()

    def _abort(self, code=1006, reason=""):
        if self.closed:
            return
        self.close_code, self.close_reason = code, reason
        self._closed.set()
        with self._incoming:
            self._incoming.notify_all()
        # Discard already-arrived input before closing: unread TCP data can
        # otherwise turn the best-effort close frame into an immediate reset.
        try:
            remaining = 65536
            while remaining:
                chunk = self._sock.recv(min(4096, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def _wait(self, writing=False, deadline=None):
        while not self.closed:
            if not writing and not self._close_sent:
                self._heartbeat()
            end = deadline
            if self._close_deadline is not None:
                end = min(end, self._close_deadline) if end is not None else self._close_deadline
            remaining = 0.2 if end is None else min(0.2, end - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("WebSocket I/O timed out")
            try:
                readable, writable, _ = select.select(
                    [] if writing else [self._sock], [self._sock] if writing else [], [], remaining)
            except (OSError, ValueError) as error:
                raise ConnectionError("WebSocket closed") from error
            if readable or writable:
                return
        raise ConnectionError("WebSocket closed")

    def _heartbeat(self):
        now = time.monotonic()
        if self._pending_ping is not None:
            if self._ping_timeout is not None and now - self._pending_ping[1] >= self._ping_timeout:
                raise _WebSocketError(1011, "keepalive pong timed out")
            if self._ping_timeout is not None:
                return
        if self._next_ping is not None and now >= self._next_ping:
            token = os.urandom(8)
            self._pending_ping = (token, now)
            self._next_ping = now + self._ping_interval
            self._frame(9, token)

    def _read_exact(self, size, *, idle=False):
        # Keep bytes across waits. An idle connection has no deadline; once a
        # frame starts, a stalled peer gets a bounded I/O deadline.
        if size == 0:
            return b""
        deadline = None if idle and not self._buffer else time.monotonic() + self.timeout
        while len(self._buffer) < size:
            self._wait(deadline=deadline)
            try:
                chunk = self._sock.recv(min(65536, size - len(self._buffer)))
            except (BlockingIOError, InterruptedError):
                continue
            if not chunk:
                raise ConnectionError("WebSocket EOF")
            if not self._buffer and len(chunk) == size:
                return chunk
            self._buffer.extend(chunk)
            if deadline is None:
                deadline = time.monotonic() + self.timeout
        with memoryview(self._buffer)[:size] as view:
            result = bytes(view)
        del self._buffer[:size]
        return result

    def _frame(self, opcode, payload, timeout=None, *, closing=False):
        parts = payload if isinstance(payload, tuple) else (memoryview(payload),)
        length = sum(part.nbytes for part in parts)
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        if not self._send_lock.acquire(timeout=max(0, timeout)):
            raise TimeoutError("WebSocket sender busy")
        data = None
        try:
            if self.closed or (self._close_sent and not closing):
                raise ConnectionError("WebSocket is closing")
            if opcode == 8 and self._close_sent:
                return
            header = (struct.pack("!BB", 0x80 | opcode, length) if length < 126 else
                      struct.pack("!BBH", 0x80 | opcode, 126, length) if length < 65536 else
                      struct.pack("!BBQ", 0x80 | opcode, 127, length))
            # One frame, multiple writes. Never concatenate the application
            # payload; the send lock and deadline cover every part together.
            for part in (memoryview(header),) + parts:
                data = part
                while data:
                    self._wait(writing=True, deadline=deadline)
                    try:
                        sent = self._sock.send(data)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not sent:
                        raise ConnectionError("WebSocket EOF")
                    data = data[sent:]
            if opcode == 8:
                self._close_sent = True
        except OSError:
            self._abort()
            raise
        finally:
            if data is not None:
                data.release()
            self._send_lock.release()

    def send(self, data):
        """Send one message, optionally from a list/tuple of binary buffers.

        Binary buffers aren't copied or joined: keep their contents unchanged
        until send returns. Non-contiguous memoryviews aren't supported.
        """
        if isinstance(data, str):
            opcode, values = 1, (data.encode("utf-8"),)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            opcode, values = 2, (data,)
        elif isinstance(data, (list, tuple)):
            values = tuple(data)
            if len(values) > _WS_MAX_PARTS:
                raise ValueError(f"WebSocket.send accepts at most {_WS_MAX_PARTS} buffers")
            opcode = 2
        else:
            raise TypeError("WebSocket.send expects str, bytes, or a list/tuple of binary buffers")
        parts, length = [], 0
        try:
            for value in values:
                if not isinstance(value, (bytes, bytearray, memoryview)):
                    raise TypeError("WebSocket.send parts must be binary buffers")
                try:
                    view = memoryview(value).cast("B")
                except (TypeError, ValueError):
                    raise TypeError("WebSocket.send requires contiguous binary buffers") from None
                parts.append(view)
                length += view.nbytes
                if length > self.max_bytes:
                    raise ValueError("WebSocket message exceeds max_bytes")
            self._frame(opcode, tuple(parts))
        finally:
            for part in parts:
                part.release()

    def ping(self, payload=b""):
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("ping payload must be bytes")
        if len(payload) > 125:
            raise ValueError("ping payload exceeds 125 bytes")
        self._frame(9, bytes(payload))

    def _next_message(self):
        while not self.closed:
            first, second = self._read_exact(2, idle=True)
            fin, opcode, length = bool(first & 128), first & 15, second & 127
            control = opcode >= 8
            if first & 112 or not second & 128 or opcode not in (0, 1, 2, 8, 9, 10):
                raise _WebSocketError(1002, "invalid frame")
            if control and (not fin or length > 125):
                raise _WebSocketError(1002, "invalid control frame")
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
                if length < 126:
                    raise _WebSocketError(1002, "non-minimal length")
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
                if length < 65536 or length & (1 << 63):
                    raise _WebSocketError(1002, "invalid length")
            if not control:
                if (opcode == 0) != (self._fragment_opcode is not None):
                    raise _WebSocketError(1002, "invalid continuation")
                if length + len(self._fragment) > self.max_bytes:
                    raise _WebSocketError(1009, "message too large")
            mask = self._read_exact(4)
            payload = _ws_unmask(self._read_exact(length), mask)
            if opcode == 8:
                if len(payload) == 1:
                    raise _WebSocketError(1002, "invalid close payload")
                code = struct.unpack("!H", payload[:2])[0] if payload else 1005
                if payload and not _ws_close_code(code):
                    raise _WebSocketError(1002, "invalid close code")
                try:
                    reason = payload[2:].decode("utf-8")
                except UnicodeDecodeError:
                    raise _WebSocketError(1007, "invalid close reason") from None
                if not self._close_sent:
                    self._frame(8, payload, timeout=1, closing=True)
                self._abort(code, reason)
                return None
            if opcode == 9:
                self._frame(10, payload, closing=True)
                continue
            if opcode == 10:
                if self._pending_ping is not None and payload == self._pending_ping[0]:
                    self._pending_ping = None
                continue
            if self._close_sent:
                continue
            if fin and opcode in (1, 2):
                # Complete messages already own their immutable unmasked payload.
                message = payload
            else:
                if opcode in (1, 2):
                    self._fragment_opcode = opcode
                self._fragment.extend(payload)
                if not fin:
                    continue
                opcode, message = self._fragment_opcode, bytes(self._fragment)
                self._fragment_opcode, self._fragment = None, bytearray()
            self._message_bytes = len(message)
            if opcode == 1:
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError:
                    raise _WebSocketError(1007, "invalid text") from None
            return message
        return None

    def _read_loop(self):
        try:
            while not self.closed:
                message = self._next_message()
                if message is None:
                    break
                with self._incoming:
                    if len(self._messages) >= self._max_queue:
                        raise _WebSocketError(1013, "incoming message queue full")
                    if self._queued_bytes + self._message_bytes > self._max_queue_bytes:
                        raise _WebSocketError(1013, "incoming byte queue full")
                    self._messages.append((message, self._message_bytes))
                    self._queued_bytes += self._message_bytes
                    self._incoming.notify()
        except _WebSocketError as error:
            try:
                self._frame(8, struct.pack("!H", error.code), timeout=1, closing=True)
            except OSError:
                pass
            self._abort(error.code, error.reason)
        except OSError:
            self._abort()
        except Exception:
            log.exception("WebSocket reader failed")
            self._abort(1011, "reader failed")
        finally:
            if not self.closed:
                self._abort()

    def receive(self):
        """Wait for a complete message; return None after queued messages and closure."""
        if not self._receive_lock.acquire(blocking=False):
            raise RuntimeError("WebSocket permits only one receiver")
        try:
            with self._incoming:
                while not self._messages and not self.closed:
                    self._incoming.wait()
                if not self._messages:
                    return None
                message, size = self._messages.popleft()
                self._queued_bytes -= size
                return message
        finally:
            self._receive_lock.release()

    def recv(self):
        """receive() convenience variant that raises ConnectionError on closure."""
        message = self.receive()
        if message is None:
            raise ConnectionError("WebSocket closed")
        return message

    def close(self, code=1000, reason="", *, timeout=1):
        """Bounded closing handshake. Safe to call repeatedly or from another thread."""
        if not _ws_close_code(code) or code == 1010:
            raise ValueError("invalid server close code")
        reason_bytes = reason.encode("utf-8")
        if len(reason_bytes) > 123:
            raise ValueError("close reason exceeds 123 UTF-8 bytes")
        if self.closed:
            return
        self._close_deadline = time.monotonic() + timeout
        try:
            if not self._close_sent:
                self._frame(8, struct.pack("!H", code) + reason_bytes, timeout=timeout, closing=True)
            self._closed.wait(max(0, self._close_deadline - time.monotonic()))
        except (OSError, _WebSocketError):
            pass
        finally:
            self._abort(code, reason)
            if threading.current_thread() is not self._reader_thread:
                self._reader_thread.join(timeout=1)

    def __iter__(self):
        while True:
            message = self.receive()
            if message is None:
                return
            yield message


class _WebSocketUpgrade:
    def __init__(self, req, handler, options):
        self.req, self.handler = req, handler
        self.options = options


# Responses and large files ---------------------------------------------------

class Response:
    """A body with explicit status, content type and optional headers."""

    def __init__(self, body=b"", status=200, content_type=None, headers=None):
        self.body, self.status = body, status
        self.headers = dict(headers or {})
        if content_type:
            self.headers["Content-Type"] = content_type


def file_response(req, path):
    """Stream a trusted file path, with HEAD and a single byte range for seeking.

    The caller must restrict paths to files it intends to expose. Static-file
    serving does its own containment check before calling this function.
    """
    path = Path(path)
    if not path.is_file():
        raise HTTPError(404, "file not found")
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    byte_range = req.headers.get("Range")
    if byte_range:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
        try:
            if not match or not any(match.groups()) or not size:
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                suffix = int(last)
                if suffix <= 0:
                    raise ValueError
                start = max(0, size - suffix)
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(0, end - start + 1))

    def chunks():
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    kind = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return Response(Stream(chunks(), kind), status, kind, headers)


class App:
    def __init__(self, static_dir=None, max_body=1024 * 1024, same_origin=True):
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.max_body = max_body
        self.routes = {}  # path -> {method: handler}
        self.websocket_routes = {}
        self.mounts = {}
        self.same_origin = same_origin
        self.components = []

    # Routes

    def route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes.setdefault(path, {})[method] = handler
            return handler
        return decorator

    def get(self, path):
        return self.route(path, ["GET"])

    def post(self, path):
        return self.route(path, ["POST"])

    def websocket(self, path, *, max_bytes=1048576, timeout=10,
                  ping_interval=20, ping_timeout=20, max_queue=256, max_queue_bytes=None,
                  origins=None):
        """Register handler(ws); timeout bounds I/O stalls, not idle receive time."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        for name, value in (("timeout", timeout), ("ping_interval", ping_interval), ("ping_timeout", ping_timeout)):
            if value is None and name != "timeout":
                continue
            if type(value) not in (int, float) or not 0 < value < float("inf"):
                raise ValueError(f"{name} must be positive and finite")
        if type(max_queue) is not int or max_queue <= 0:
            raise ValueError("max_queue must be a positive integer")
        if max_queue_bytes is not None and (type(max_queue_bytes) is not int or max_queue_bytes <= 0):
            raise ValueError("max_queue_bytes must be a positive integer or None")
        allowed_origins = None
        if origins is not None:
            if isinstance(origins, (str, bytes)):
                raise ValueError("origins must be a collection of origins, not a string")
            allowed_origins = frozenset(origins)
            for origin in allowed_origins:
                if origin is None:
                    continue
                if not isinstance(origin, str):
                    raise ValueError("origins entries must be HTTP(S) origins or None")
                parsed = urllib.parse.urlsplit(origin)
                if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                        or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
                    raise ValueError("origins entries must be HTTP(S) origins without a path")
                parsed.port  # validate port syntax/range at registration, not during requests
        def register(handler):
            if path in self.websocket_routes:
                raise ValueError("duplicate WebSocket route")
            self.websocket_routes[path] = (handler, dict(max_bytes=max_bytes, timeout=timeout,
                ping_interval=ping_interval, ping_timeout=ping_timeout, max_queue=max_queue,
                max_queue_bytes=max_queue_bytes), allowed_origins)
            return handler
        return register

    def mount(self, prefix, app):
        """Mount an app on a path boundary; longest matching prefix wins."""
        prefix = prefix.rstrip("/")
        if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", prefix):
            raise ValueError("mount prefix must be a non-root path such as /downloads")
        if prefix in self.mounts or app is self:
            raise ValueError("duplicate mount or self mount")
        self.mounts[prefix] = app
        return app

    # Running

    def run(self, host, port, on_ready=None, *, stop_event=None, components=None):
        """Bind, start components, serve, drain requests, then stop in reverse order.

        stop_event is an optional threading.Event for an embedding host or tests.
        Plain App.run remains valid without any components.
        """
        components = list(self.components if components is None else components)
        if len({c.id for c in components}) != len(components):
            raise ValueError("duplicate component id")
        if any(c._attempted for c in components):
            raise RuntimeError("component instances support only one host run")
        try:
            server = _Server((host, port), self._handler_class())
        except OSError as err:
            if err.errno == errno.EADDRINUSE or getattr(err, "winerror", None) == 10048:
                raise SystemExit(f"error: port {port} on {host} is already in use") from None
            raise
        attempted, cleanup_errors = [], []
        primary = None
        try:
            for component in components:
                attempted.append(component)
                component.start()
            if on_ready:
                on_ready()
            server.timeout = 0.2
            while stop_event is None or not stop_event.is_set():
                server.handle_request()
        except KeyboardInterrupt:
            pass
        except BaseException as error:
            primary = error
        finally:
            # Stop admission first; existing request threads retain their sockets.
            try:
                server.socket.close()
                server.stop_websockets()
                server.drain_requests()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    log.error("HTTP drain failed during cleanup", exc_info=True)
            for component in reversed(attempted):
                try:
                    component.stop()
                except BaseException as error:
                    cleanup_errors.append(error)
                    log.error("Component %s failed to stop", component.id, exc_info=True)
            try:
                server.server_close()
            except BaseException as error:
                cleanup_errors.append(error)
        if primary is not None:
            raise primary
        if cleanup_errors:
            raise RuntimeError("Host cleanup failed; see component errors") from cleanup_errors[0]

    def handle(self, method, path, body=b"", headers=None):
        """Run a request through the app without a network connection, e.g.
        for tests. Returns (status, headers dict, body bytes)."""
        message = http.client.HTTPMessage()
        for name, value in (headers or {}).items():
            message[name] = value
        if body and "Content-Length" not in message:
            message["Content-Length"] = str(len(body))
        req = Request(method, path, message, io.BytesIO(body), "test", self.max_body)
        status, response_headers, data = self._dispatch(req)
        if isinstance(data, _WebSocketUpgrade):
            # The offline helper validates the handshake but has no transport.
            data = b""
        if isinstance(data, Stream):
            data = b"".join(data) if method != "HEAD" else b""
        if method == "HEAD":
            data = b""
        return status, dict(response_headers), data

    # Internals

    def _dispatch(self, req):
        """Route a request; returns (status, [(header, value)], body bytes)."""
        try:
            if req.path in self.websocket_routes and (req.headers.get("Upgrade") or req.path not in self.routes):
                return self._websocket_handshake(req)
            if self.same_origin and req.method not in ("GET", "HEAD", "OPTIONS"):
                origin = req.headers.get("Origin")
                if origin and not _allowed_origin(origin, req.headers.get("Host", "")):
                    raise HTTPError(403, "cross-origin request refused")
            handlers = self.routes.get(req.path)
            if handlers:
                handler = handlers.get(req.method)
                if handler is None and req.method == "HEAD":
                    handler = handlers.get("GET")
                if handler is None:
                    status, headers, body = self._json(405, {"error": "method not allowed"})
                    return status, headers + [("Allow", ", ".join(sorted(handlers)))], body
                return self._response(handler(req))
            for prefix, child in sorted(self.mounts.items(), key=lambda item: -len(item[0])):
                if req.path == prefix:
                    location = req.root_path + prefix + "/"
                    if req.query_string:
                        location += "?" + req.query_string
                    return self._response(Response(status=307, headers={"Location": location}))
                if req.path.startswith(prefix + "/"):
                    mounted = copy.copy(req)
                    mounted.path = req.path[len(prefix):]
                    mounted.root_path = req.root_path + prefix
                    mounted._max_body = child.max_body
                    return child._dispatch(mounted)
            if req.method in ("GET", "HEAD") and self.static_dir:
                static = self._static(req.path)
                if static:
                    return self._response(file_response(req, static))
            return self._json(404, {"error": "not found"})
        except HTTPError as err:
            # Raised on purpose by a handler (which logs anything worth logging).
            log.debug("%s %s -> %d: %s", req.method, req.path, err.status, err.message)
            return self._json(err.status, {"error": err.message, **err.extra})
        except Exception:
            log.exception("%s %s failed", req.method, req.path)
            return self._json(500, {"error": "internal error"})

    def _websocket_handshake(self, req):
        tokens = lambda name: {v.strip().lower() for v in req.headers.get(name, "").split(",")}
        if any(len(req.headers.get_all(name, [])) > 1
               for name in ("Host", "Origin", "Sec-WebSocket-Key", "Sec-WebSocket-Version")):
            raise HTTPError(400, "duplicate WebSocket handshake header")
        if req.method != "GET":
            return self._response(Response("WebSocket requires GET", 405, headers={"Allow": "GET"}))
        if req.headers.get("Sec-WebSocket-Version") != "13":
            return self._response(Response("WebSocket version 13 required", 426,
                                           headers={"Sec-WebSocket-Version": "13"}))
        if ("websocket" not in tokens("Upgrade") or "upgrade" not in tokens("Connection")
                or not req.headers.get("Host") or getattr(req, "_http_version", "HTTP/1.1") != "HTTP/1.1"
                or "Transfer-Encoding" in req.headers or req.headers.get("Content-Length", "0") != "0"):
            raise HTTPError(400, "invalid WebSocket upgrade")
        handler, options, allowed_origins = self.websocket_routes[req.path]
        origin = req.headers.get("Origin")
        if allowed_origins is not None:
            if origin not in allowed_origins:
                raise HTTPError(403, "WebSocket origin not allowed")
        elif self.same_origin and origin and not _allowed_origin(origin, req.headers.get("Host", "")):
            raise HTTPError(403, "cross-origin WebSocket refused")
        try:
            key = req.headers.get("Sec-WebSocket-Key", "")
            if len(base64.b64decode(key, validate=True)) != 16:
                raise ValueError
        except (ValueError, UnicodeError):
            raise HTTPError(400, "invalid WebSocket key") from None
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        return 101, [("Upgrade", "websocket"), ("Connection", "Upgrade"),
                     ("Sec-WebSocket-Accept", accept)], _WebSocketUpgrade(req, handler, options)

    def _response(self, result):
        if isinstance(result, Response):
            _, headers, body = self._response(result.body)
            combined = dict(headers)
            for name, value in result.headers.items():
                for old in list(combined):
                    if old.lower() == name.lower():
                        del combined[old]
                combined[name] = value
            return result.status, list(combined.items()), body
        status = 200
        if isinstance(result, tuple):
            result, status = result
        if result is None:
            return (204 if status == 200 else status), [], b""
        if isinstance(result, (dict, list)):
            return self._json(status, result)
        if isinstance(result, str):
            return status, [("Content-Type", "text/plain; charset=utf-8")], result.encode("utf-8")
        if isinstance(result, bytes):
            return status, [("Content-Type", "application/octet-stream")], result
        if isinstance(result, Stream):
            return status, [("Content-Type", result.content_type), ("Cache-Control", "no-cache")], result
        raise TypeError(f"handler returned unsupported type {type(result).__name__}")

    @staticmethod
    def _json(status, payload):
        return status, [("Content-Type", "application/json")], json.dumps(payload).encode("utf-8")

    def _static(self, path):
        """A file from static_dir, or None. Never lists directories or leaves static_dir."""
        relative = "index.html" if path == "/" else path.lstrip("/")
        file = (self.static_dir / relative).resolve()
        if not file.is_relative_to(self.static_dir) or not file.is_file():
            return None
        return file

    def _handler_class(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "microweb"

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    with self.server._requests:
                        closing_idle = (self.server._stopping and
                                        self.connection in self.server._pending)
                    if not closing_idle:
                        raise

            def _serve(self):
                with self.server._requests:
                    if self.server._stopping:
                        self.close_connection = True
                        return
                    self.server._pending.discard(self.connection)
                req = Request(self.command, self.path, self.headers, self.rfile,
                              self.client_address[0], app.max_body)
                req._http_version = self.request_version
                status, headers, body = app._dispatch(req)
                if isinstance(body, _WebSocketUpgrade):
                    self._serve_websocket(headers, body)
                    return
                streaming = isinstance(body, Stream)
                try:
                    self.send_response(status)
                    for name, value in headers:
                        self.send_header(name, value)
                    if not streaming and not any(name.lower() == "content-length" for name, _ in headers):
                        self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command == "HEAD":
                        return
                    if not streaming:
                        self.wfile.write(body)
                        return
                    try:
                        for chunk in body:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        raise
                    except Exception:
                        # Headers are already sent, so all we can do is stop.
                        log.exception("%s %s failed while streaming", req.method, req.path)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True
                    log.debug("%s %s: client disconnected", req.method, req.path)

            def _serve_websocket(self, headers, upgrade):
                self.close_connection = True
                ws = None
                try:
                    self.protocol_version = "HTTP/1.1"
                    self.send_response(101)
                    for name, value in headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.flush()
                    if self.connection.family in (socket.AF_INET, socket.AF_INET6):
                        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.connection.setblocking(False)
                    # Preserve any bytes already buffered by the HTTP reader.
                    initial = self.rfile.read1(65536)
                    ws = WebSocket(self.connection, upgrade.req, initial=initial, **upgrade.options)
                    with self.server._requests:
                        if self.server._stopping:
                            ws._abort(1001, "server stopping")
                            return
                        self.server._websockets.add(ws)
                    try:
                        upgrade.handler(ws)
                    except OSError:
                        ws._abort()
                    except Exception:
                        log.exception("WebSocket %s failed", self.path)
                        ws.close(1011, "handler failed")
                except OSError:
                    if ws:
                        ws._abort()
                finally:
                    if ws:
                        ws.close()
                        with self.server._requests:
                            self.server._websockets.discard(ws)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, format, *args):
                log.debug("%s %s", self.address_string(), format % args)

        return Handler


# Optional components and console shell --------------------------------------

class Component:
    """An independently hostable app and its owned service lifecycle."""

    def __init__(self, id, label, app, start=None, stop=None):
        self.id, self.label, self.app = id, label, app
        self._start, self._stop = start, stop
        self._attempted = self._stopped = False

    def start(self):
        if self._attempted:
            raise RuntimeError("component instances support only one host run")
        self._attempted = True
        if self._start:
            self._start()

    def stop(self):
        if not self._attempted or self._stopped:
            return
        self._stopped = True
        if self._stop:
            self._stop()

    def run(self, host, port, on_ready=None, *, stop_event=None):
        return self.app.run(host, port, on_ready, stop_event=stop_event, components=[self])

class Shell(App):
    """A landing page and persistent navigation around independent mounted apps.

    Apps run inside a same-origin iframe, keeping their HTML and assets separate.
    Visiting a mounted URL directly still serves the app without the shell.
    """

    STYLE = """
    body { margin:0; font:16px system-ui,sans-serif; color:#243047; background:#f4f6fa }
    header { padding:16px 24px; background:#fff; border-bottom:1px solid #dce1ea }
    nav { display:flex; flex-wrap:wrap; gap:16px; align-items:center }
    a { color:#2457aa } nav strong { margin-right:12px }
    main { max-width:1120px; margin:40px auto; padding:0 24px }
    .component-table { overflow-x:auto }
    table { width:100%; border-collapse:collapse; background:#fff }
    th,td { padding:16px; text-align:left; border-bottom:1px solid #dce1ea }
    th { font-size:13px; color:#57657a; background:#edf1f7 }
    td code,.muted { font-size:13px; color:#57657a }
    nav a[aria-current] { font-weight:600 }
    iframe { width:100%; border:0; background:white }
    iframe.panel { flex:1; min-height:0 }
    iframe.banner { display:block; flex-shrink:0; border-bottom:1px solid #dce1ea }
    header { flex-shrink:0 }
    body.focus { height:100vh; display:flex; flex-direction:column }
    """

    def __init__(self, title="Tools", **kwargs):
        super().__init__(**kwargs)
        self.title = title
        self.tools = {}
        self.banners = {}
        self._entries = {}
        self.get("/")(self._home)

    def mount(self, prefix, app, label=None, *, description=""):
        super().mount(prefix, app)
        self.tools[prefix.rstrip("/")] = label or prefix.strip("/")
        self._entries[prefix.rstrip("/")] = dict(id=prefix.strip("/"),
            label=label or prefix.strip("/"), description=description, placement="panel")
        return app

    def add(self, component, prefix, *, placement="panel", height=96, description=""):
        """Mount an app and explicitly register its lifecycle. Construction is quiet."""
        if any(c.id == component.id or c is component for c in self.components):
            raise ValueError("duplicate component id")
        if placement not in ("panel", "banner"):
            raise ValueError("placement must be panel or banner")
        if type(height) is not int or height <= 0:
            raise ValueError("height must be a positive integer in pixels")
        if placement == "banner":
            super().mount(prefix, component.app)
            self.banners[prefix.rstrip("/")] = (component.label, height)
        else:
            self.mount(prefix, component.app, label=component.label)
        self._entries[prefix.rstrip("/")] = dict(id=component.id, label=component.label,
            description=description, placement=placement)
        self.components.append(component)
        return component

    def _home(self, req):
        selected = req.query.get("tool")
        if selected is not None and selected not in self.tools:
            raise HTTPError(404, "unknown tool")
        title = html.escape(self.title)
        home = html.escape(req.url(), quote=True)
        links = []
        for prefix, label in self.tools.items():
            href = html.escape(req.url() + "?" + urllib.parse.urlencode({"tool": prefix}), quote=True)
            current = ' aria-current="page"' if selected == prefix else ''
            link = f'<a href="{href}"{current}>{html.escape(label)}</a>'
            links.append(link)
        if selected:
            src = html.escape(req.url(selected + "/"), quote=True)
            content = f'<iframe title="{html.escape(self.tools[selected], quote=True)}" src="{src}" class="panel"></iframe>'
        else:
            content = '<main><h1>Components</h1><p>Registered views in this console.</p>'
            if self._entries:
                content += ('<div class="component-table"><table><thead><tr>'
                            '<th scope="col">Component</th><th scope="col">Purpose</th>'
                            '<th scope="col">Placement</th><th scope="col">Mount</th>'
                            '</tr></thead><tbody>')
                for prefix, entry in self._entries.items():
                    mount = req.url(prefix + "/")
                    url = (mount if entry["placement"] == "banner" else
                           req.url() + "?" + urllib.parse.urlencode({"tool": prefix}))
                    content += ('<tr><td><strong><a href="' + html.escape(url, quote=True) + '">' +
                                html.escape(entry["label"]) + '</a></strong><br><code>' +
                                html.escape(entry["id"]) + '</code></td><td>' +
                                html.escape(entry["description"] or "—") + '</td><td>' +
                                ('Shared banner' if entry["placement"] == "banner" else 'Panel') +
                                '</td><td><code>' + html.escape(mount) + '</code></td></tr>')
                content += '</tbody></table></div><p class="muted">This lists registrations, not component health checks.</p>'
            else:
                content += '<p>No tools registered yet. Mount an App to add one.</p>'
            content += '</main>'
        banners = ''.join(
            f'<iframe class="banner" title="{html.escape(label, quote=True)}" '
            f'src="{html.escape(req.url(prefix + "/"), quote=True)}" style="height:{height}px"></iframe>'
            for prefix, (label, height) in self.banners.items())
        page = (f'<!doctype html><html lang="en"><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{title}</title><style>{self.STYLE}</style>'
                f'<body class="{"focus" if selected else "home"}"><header><nav aria-label="Primary navigation">'
                f'<strong><a href="{home}"' + (' aria-current="page"' if selected is None else '') +
                f'>{title}</a></strong>' + "".join(links) +
                '</nav></header>' + banners + content + '</body></html>')
        return Response(page, content_type="text/html; charset=utf-8")


# Explicit version checks and vendored-file updates --------------------------

def _version_parts(version):
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("release versions must be vMAJOR.MINOR.PATCH")
    return tuple(int(part) for part in version[1:].split("."))


def _download(url):
    if not url.startswith("https://"):
        raise ValueError("update URLs must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "microweb-updater"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not response.url.startswith("https://"):
            raise ValueError("update redirected away from HTTPS")
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("update response exceeds 2 MB")
        return data


def check_update():
    """Read the announcement on main. Never called during import or app startup."""
    if not UPDATE_URL:
        raise ValueError("development copy: no release manifest URL configured")
    manifest = json.loads(_download(UPDATE_URL))
    _version_parts(manifest["version"])
    if not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise ValueError("invalid release SHA-256")
    return manifest


def install_update(manifest, target=None):
    """Install the requested release, backing up current bytes including local edits.

    Verify incoming bytes before replacing anything. No downloaded code is run.
    """
    current = _version_parts(__version__)
    if _version_parts(manifest["version"]) < current:
        return False
    target = Path(target or __file__).resolve()
    original = target.read_bytes()
    if hashlib.sha256(original).hexdigest() == manifest["sha256"]:
        return False
    backup = target.with_name(target.name + ".bak")
    if backup.exists():
        raise ValueError(f"backup already exists: {backup}; keep or remove it before updating")
    incoming = _download(manifest["url"])
    if hashlib.sha256(incoming).hexdigest() != manifest["sha256"]:
        raise ValueError("downloaded file does not match release SHA-256")
    tree = ast.parse(incoming)
    versions = [node.value.value for node in tree.body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(name, ast.Name) and name.id == "__version__" for name in node.targets)]
    if versions != [manifest["version"]]:
        raise ValueError("downloaded file version does not match manifest")
    compile(incoming, str(target), "exec")
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".microweb-", delete=False) as output:
            staged = Path(output.name)
            output.write(incoming)
        staged.chmod(target.stat().st_mode)
        if target.read_bytes() != original:
            raise ValueError("local file changed during update")
        with backup.open("xb") as output:
            output.write(original)
        staged.replace(target)
        return True
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Single-file local web apps and optional console shell")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--version", action="store_true")
    actions.add_argument("--check-update", action="store_true")
    actions.add_argument("--update", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return
    try:
        remote = check_update()
        newer = _version_parts(remote["version"]) > _version_parts(__version__)
        print(f'Installed: {__version__}; latest release: {remote["version"]}')
        if args.update:
            if install_update(remote):
                print("Updated. Previous file (including local edits) saved as microweb.py.bak; restart applications to use the new version.")
            else:
                print("No update needed; already matches the latest release or has a newer version.")
        elif newer:
            print("An update is available. Run with --update to install it.")
        else:
            print("No newer release available.")
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        parser.exit(1, f"Update failed: {error}\n")


if __name__ == "__main__":
    main()
