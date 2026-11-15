"""Standalone mono PCM szaL16 reference; see README.md and ../szaL16.md.

Run these blocking helpers in worker threads with caller-supplied connections.
recv()/send() are synchronous, as in websockets.sync.
relay() also uses send(iterable_of_buffers) to fragment ONE binary WebSocket message.
"""

import base64
import json
import struct
import sys
import threading
import time
import wave
from array import array


def _format(start):
    if start["type"] != "audio" or start["codec"] != "pcm_s16le":
        raise ValueError("Expected pcm_s16le")
    rate, frames, frame_ms = struct.unpack("<HBB", base64.b64decode(start["codec_header"], validate=True))
    duration = frames * frame_ms
    if not rate or frames not in (1, 2) or not frame_ms or not 2.5 <= duration <= 60:
        raise ValueError("Format cannot be represented by the szaL16 header")
    if start["packet_duration"] != duration or rate * frame_ms % 1000:
        raise ValueError("Inconsistent duration or fractional sample count")
    return rate, rate * duration // 1000 * 2


def _payload(packet, stream_id, size):
    if len(packet) != 9 + size:
        raise ValueError("Unexpected audio packet length")
    kind, actual_id, _ = struct.unpack_from("!BII", packet)
    if kind != 1 or actual_id != stream_id:
        raise ValueError("Unexpected audio packet type or stream_id")
    return memoryview(packet)[9:]


def _native_le16(data):
    # wave reads/writes native-endian sample buffers; LE hosts need no conversion.
    if sys.byteorder == "little":
        return data
    samples = array("h")
    samples.frombytes(data)
    samples.byteswap()
    return samples.tobytes()


def relay(source, destinations, *, stream_id, sender):
    """Relay one outgoing stream to caller-selected receivers, then return.

    source has recv()/send(); destinations is a fixed iterable of receiver sockets.
    The caller assigns a unique uint32 stream_id and the sender's public identity.
    No audio conversion; one payload memoryview is shared across outgoing sends.
    """
    destinations = tuple(destinations)
    start = json.loads(source.recv())
    if start["command"] != "start_stream":
        raise ValueError("Expected start_stream")
    _, size = _format(start)
    event = {key: start[key] for key in ("channel", "type", "codec", "codec_header", "packet_duration", "for") if key in start}
    event.update(command="on_stream_start", stream_id=stream_id)
    event["from"] = sender
    started = []
    try:
        for target in destinations:
            target.send(json.dumps(event))
            started.append(target)
        if "seq" in start:
            source.send(json.dumps(dict(seq=start["seq"], success=True, stream_id=stream_id)))
        packet_id = 0
        while True:
            message = source.recv()
            if isinstance(message, str):
                stop = json.loads(message)
                if stop["command"] != "stop_stream" or stop["stream_id"] != stream_id:
                    raise ValueError("Expected stop_stream for the active stream")
                if "seq" in stop:
                    source.send(json.dumps(dict(seq=stop["seq"], success=True)))
                return
            payload = _payload(message, stream_id, size)
            header = struct.pack("!BII", 1, stream_id, packet_id)
            for target in destinations:
                target.send((header, payload))
            packet_id = (packet_id + 1) & 0xffffffff
    finally:
        # Receivers get a stop event on normal stop, disconnect or malformed input.
        stop = json.dumps(dict(command="on_stream_stop", stream_id=stream_id))
        failure = sys.exception()
        cleanup_error = None
        for target in started:
            try:
                target.send(stop)
            except Exception as error:
                cleanup_error = cleanup_error or error
        if failure is None and cleanup_error is not None:
            raise cleanup_error


def write_wav(source, path):
    """Receive one on_stream_start/audio/on_stream_stop sequence into a PCM WAV.

    Returns the sample count written, including final-packet silence padding.
    A disconnected/malformed stream raises; the WAV header is still finalized.
    """
    start = json.loads(source.recv())
    if start["command"] != "on_stream_start":
        raise ValueError("Expected on_stream_start")
    rate, size = _format(start)
    count = 0
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        while True:
            message = source.recv()
            if isinstance(message, str):
                stop = json.loads(message)
                if stop["command"] != "on_stream_stop" or stop["stream_id"] != start["stream_id"]:
                    raise ValueError("Expected on_stream_stop for the active stream")
                return count
            payload = _payload(message, start["stream_id"], size)
            output.writeframesraw(_native_le16(payload))
            count += len(payload) // 2


def read_wav(socket, path, *, channel, frame_ms=16, frames_per_packet=1, seq=1, stop=None):
    """Send one mono PCM16 WAV in real time; silence-pad the last partial packet.

    The connection is dedicated to this exchange; its next message is the start reply.
    No resampling, channel mixing or conversion of other WAV sample encodings.
    An optional threading.Event interrupts pacing; close the socket to unblock recv/send.
    """
    stop = stop if stop is not None else threading.Event()
    if stop.is_set():
        return
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("Expected an uncompressed mono PCM16 WAV")
        rate = source.getframerate()
        start = dict(command="start_stream", seq=seq, channel=channel, type="audio", codec="pcm_s16le",
                     codec_header=base64.b64encode(struct.pack("<HBB", rate, frames_per_packet, frame_ms)).decode("ascii"),
                     packet_duration=frame_ms * frames_per_packet)
        _, size = _format(start)
        socket.send(json.dumps(start))
        reply = json.loads(socket.recv())
        if reply.get("seq") != seq or not reply.get("success") or "stream_id" not in reply:
            raise ValueError(reply.get("error", "start_stream failed"))
        stream_id = reply["stream_id"]
        header = struct.pack("!BII", 1, stream_id, 0)
        deadline = time.monotonic()
        try:
            while not stop.is_set() and (data := source.readframes(size // 2)):
                data = data.ljust(size, b"\0")
                socket.send((header, _native_le16(data)))
                deadline += start["packet_duration"] / 1000
                stop.wait(max(0, deadline - time.monotonic()))
        finally:
            socket.send(json.dumps(dict(command="stop_stream", stream_id=stream_id, channel=channel)))
