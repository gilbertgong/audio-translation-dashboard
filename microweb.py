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
    (body, status)    -> any of the above with that status
raise HTTPError(status, message, **extra) -> JSON {"error": message, **extra}.
Any other exception -> 500 {"error": "internal error"}, traceback logged.

Lookup order: exact-path route; for GET, a file from static_dir ("/" serves
index.html; no directory listings); else 404. A known path with the wrong
method gets 405 with an Allow header. Request bodies need a Content-Length
(411 otherwise) and are limited to max_body bytes (1 MB by default, raised per
call with req.body(max_bytes=...)).

Each request runs in its own thread, and each connection serves one request
(HTTP/1.0 style). Logging goes to the "microweb" logger: unexpected exceptions
as errors with a traceback; request lines and HTTPErrors at debug level (a
handler that raises HTTPError logs what's worth logging itself).
"""

import errno
import http.client
import io
import json
import logging
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    ".woff2": "font/woff2",
}


class HTTPError(Exception):
    """Raise in a handler to send an error response: JSON {"error": message, **extra}."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


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
        self.query = dict(urllib.parse.parse_qsl(url.query))
        self.headers = headers
        self.client = client
        self._rfile = rfile
        self._max_body = max_body
        self._body = None

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


class App:
    def __init__(self, static_dir=None, max_body=1024 * 1024):
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.max_body = max_body
        self.routes = {}  # path -> {method: handler}

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

    # Running

    def run(self, host, port, on_ready=None):
        """Serve until Ctrl+C. on_ready() is called once the port is open."""
        try:
            server = _Server((host, port), self._handler_class())
        except OSError as err:
            if err.errno == errno.EADDRINUSE or getattr(err, "winerror", None) == 10048:
                raise SystemExit(f"error: port {port} on {host} is already in use") from None
            raise
        try:
            if on_ready:
                on_ready()
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()

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
        return status, dict(response_headers), data

    # Internals

    def _dispatch(self, req):
        """Route a request; returns (status, [(header, value)], body bytes)."""
        try:
            handlers = self.routes.get(req.path)
            if handlers:
                handler = handlers.get(req.method)
                if handler is None:
                    status, headers, body = self._json(405, {"error": "method not allowed"})
                    return status, headers + [("Allow", ", ".join(sorted(handlers)))], body
                return self._response(handler(req))
            if req.method in ("GET", "HEAD") and self.static_dir:
                static = self._static(req.path)
                if static:
                    return static
            return self._json(404, {"error": "not found"})
        except HTTPError as err:
            # Raised on purpose by a handler (which logs anything worth logging).
            log.debug("%s %s -> %d: %s", req.method, req.path, err.status, err.message)
            return self._json(err.status, {"error": err.message, **err.extra})
        except Exception:
            log.exception("%s %s failed", req.method, req.path)
            return self._json(500, {"error": "internal error"})

    def _response(self, result):
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
        content_type = CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream")
        # no-cache: the browser must check back before using a cached copy, so
        # edited files are always picked up on reload.
        return 200, [("Content-Type", content_type), ("Cache-Control", "no-cache")], file.read_bytes()

    def _handler_class(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "microweb"

            def _serve(self):
                req = Request(self.command, self.path, self.headers, self.rfile,
                              self.client_address[0], app.max_body)
                status, headers, body = app._dispatch(req)
                try:
                    self.send_response(status)
                    for name, value in headers:
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    log.debug("%s %s: client disconnected", req.method, req.path)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, format, *args):
                log.debug("%s %s", self.address_string(), format % args)

        return Handler
