"""Echo endpoint checks over isolated real WebSockets; no inference or microphone."""
import base64
import contextlib
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app
from microweb import _Server


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def send(sock, value):
    data = json.dumps(value).encode() if isinstance(value, dict) else value
    kind = 1 if isinstance(value, dict) else 2
    size = len(data)
    header = bytes([0x80 | kind, 0x80 | size]) if size < 126 else bytes([0x80 | kind, 0xfe]) + struct.pack("!H", size)
    mask = b"abcd"
    sock.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))


def receive(sock):
    kind, size = exact(sock, 2)
    assert not size & 128
    if size == 126:
        size = struct.unpack("!H", exact(sock, 2))[0]
    data = exact(sock, size)
    return json.loads(data) if kind & 15 == 1 else data


@contextlib.contextmanager
def connection(server, query):
    with socket.create_connection(server.server_address, timeout=2) as sock:
        host = f"127.0.0.1:{server.server_port}"
        request = (f"GET /api/audio?{query} HTTP/1.1\r\nHost: {host}\r\nOrigin: http://{host}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
        sock.sendall(request.encode())
        response = b""
        while not response.endswith(b"\r\n\r\n"):
            response += exact(sock, 1)
        assert response.startswith(b"HTTP/1.1 101 ")
        yield sock


class EchoTests(unittest.TestCase):
    def setUp(self):
        self.server = _Server(("127.0.0.1", 0), app.app._handler_class())
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.stop_websockets()
        self.server.drain_requests()
        self.server.server_close()
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())

    def start(self, sock, channel, rate=48000, frame_ms=16, echo=True, codec="pcm_s16le"):
        header = base64.b64encode(struct.pack("<HBB", rate, 1, frame_ms)).decode()
        send(sock, dict(command="start_stream", seq=1, channel=channel, type="audio", codec=codec,
                        codec_header=header, packet_duration=frame_ms))
        reply = receive(sock)
        self.assertTrue(reply["success"])
        self.assertEqual(reply["seq"], 1)
        if echo:
            event = receive(sock)
            self.assertEqual(event["command"], "on_stream_start")
            self.assertEqual(event["channel"], channel)
            self.assertEqual(event["codec"], codec)
            self.assertEqual(event["codec_header"], header)
        return reply["stream_id"]

    def test_echo_preserves_payload_numbers_packets_and_restarts(self):
        with connection(self.server, "channel=one&echo=1") as sock:
            for rate, ms in [(48000, 16), (16000, 20)]:
                stream_id = self.start(sock, "one", rate, ms)
                payload = struct.pack("<h", -12345) * (rate * ms // 1000)
                for number in (0, 1):
                    send(sock, struct.pack("!BII", 1, stream_id, 999) + payload)
                    result = receive(sock)
                    self.assertEqual(struct.unpack_from("!BII", result), (1, stream_id, number))
                    self.assertEqual(result[9:], payload)
                send(sock, dict(command="stop_stream", seq=2, channel="one", stream_id=stream_id))
                self.assertEqual(receive(sock), dict(command="on_stream_stop", stream_id=stream_id))
                self.assertEqual(receive(sock), dict(seq=2, success=True))

    def test_echo_flag_and_connection_isolation(self):
        with connection(self.server, "channel=one&echo=1") as first, connection(self.server, "channel=two&echo=0") as second:
            first_id = self.start(first, "one")
            second_id = self.start(second, "two", echo=False)
            payload = bytes(1536)
            send(first, struct.pack("!BII", 1, first_id, 0) + payload)
            self.assertEqual(receive(first)[9:], payload)
            send(second, struct.pack("!BII", 1, second_id, 0) + payload)
            send(second, dict(command="stop_stream", seq=2, channel="two", stream_id=second_id))
            # There are no echo events/data and no audio leaked from the other connection.
            self.assertEqual(receive(second), dict(seq=2, success=True))

    def test_opus_echo_is_opaque_variable_length_and_can_switch_back_to_pcm(self):
        with connection(self.server, "channel=opus&echo=1") as sock:
            stream_id = self.start(sock, "opus", 16000, 20, codec="opus")
            # Deliberately opaque bytes: server must never invoke an Opus decoder.
            for number, payload in enumerate((b"\xf8\xff\xfe", bytes(range(200)), b"\0")):
                send(sock, struct.pack("!BII", 1, stream_id, 999) + payload)
                result = receive(sock)
                self.assertEqual(struct.unpack_from("!BII", result), (1, stream_id, number))
                self.assertEqual(result[9:], payload)
            send(sock, dict(command="stop_stream", seq=2, channel="opus", stream_id=stream_id))
            self.assertEqual(receive(sock)["command"], "on_stream_stop")
            self.assertTrue(receive(sock)["success"])
            stream_id = self.start(sock, "opus", 16000, 16)
            send(sock, struct.pack("!BII", 1, stream_id, 0) + bytes(512))
            self.assertEqual(len(receive(sock)), 521)

    def test_bad_opus_duration_or_empty_payload_closes(self):
        with connection(self.server, "channel=opus&echo=1") as sock:
            send(sock, dict(command="start_stream", channel="opus", type="audio", codec="opus",
                codec_header=base64.b64encode(struct.pack("<HBB", 16000, 1, 16)).decode(), packet_duration=16))
            self.assertEqual(struct.unpack_from("!H", receive(sock))[0], 1008)
        with connection(self.server, "channel=opus&echo=1") as sock:
            stream_id = self.start(sock, "opus", 16000, 20, codec="opus")
            send(sock, struct.pack("!BII", 1, stream_id, 0))
            self.assertEqual(struct.unpack_from("!H", receive(sock))[0], 1008)

    def test_missing_channel_wrong_channel_and_bad_packet_close(self):
        with connection(self.server, "echo=1") as sock:
            self.assertEqual(struct.unpack_from("!H", receive(sock))[0], 1008)
        with connection(self.server, "channel=one&echo=1") as sock:
            send(sock, dict(command="start_stream", channel="wrong"))
            self.assertEqual(struct.unpack_from("!H", receive(sock))[0], 1008)
        with connection(self.server, "channel=one&echo=1") as sock:
            self.start(sock, "one")
            send(sock, b"bad")
            self.assertEqual(struct.unpack_from("!H", receive(sock))[0], 1008)


if __name__ == "__main__":
    unittest.main()
