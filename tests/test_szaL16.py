"""Offline cross-language stream/WAV checks: python tests/test_szaL16.py."""

import base64
import importlib.util
import json
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("szaL16", ROOT / "docs/reference/szaL16.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


class Socket:
    def __init__(self, messages=(), acknowledge=False):
        self.messages = list(messages)
        self.sent = []
        self.fragments = []
        self.acknowledge = acknowledge

    def recv(self):
        if not self.messages:
            raise EOFError("disconnected")
        return self.messages.pop(0)

    def send(self, message):
        if not isinstance(message, (str, bytes)):
            self.fragments.append(message)
            message = b"".join(message)
        self.sent.append(message)
        if self.acknowledge and isinstance(message, str):
            control = json.loads(message)
            if control["command"] == "start_stream":
                self.messages.append(json.dumps(dict(seq=control["seq"], success=True, stream_id=42)))


def js(arguments, data=None):
    result = subprocess.run(["node", str(ROOT / "tests/test_szaL16.mjs"), *arguments],
                            input=None if data is None else json.dumps(data), text=True,
                            capture_output=True, check=True, timeout=20)
    return json.loads(result.stdout)


class ReferenceTests(unittest.TestCase):
    def test_js_to_relay_to_wav(self):
        wire = js(["--emit"])
        packets = [base64.b64decode(packet) for packet in wire["packets"]]
        self.assertEqual(wire["start"]["codec"], "pcm_s16le")
        self.assertEqual(packets[0][9:19], struct.pack("<5h", -32768, -16384, 0, 16384, 32767))
        outgoing = [json.dumps(wire["request"]), *packets,
                    json.dumps(dict(command="stop_stream", stream_id=42))]
        source, first, second = Socket(outgoing), Socket(), Socket()
        reference.relay(source, [first, second], stream_id=42, sender="alice")
        self.assertEqual(first.sent, second.sent)
        self.assertEqual(json.loads(source.sent[0]), dict(seq=7, success=True, stream_id=42))
        self.assertEqual(json.loads(first.sent[0])["from"], "alice")
        self.assertEqual(struct.unpack_from("!I", first.sent[2], 5)[0], 1)
        # The relay passes a view of the original payload, not a converted/copy buffer.
        self.assertIs(first.fragments[0][1].obj, packets[0])
        self.assertEqual(first.sent[1][9:], packets[0][9:])
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "received.wav"
            count = reference.write_wav(Socket(first.sent), path)
            self.assertEqual(count, 512)
            with wave.open(str(path), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 16000))
                samples = struct.unpack("<512h", audio.readframes(512))
            self.assertEqual(samples[:9], (-32768, -16384, 0, 16384, 32767, -32768, 32767, 1, 32767))
            self.assertTrue(all(value == 8192 for value in samples[9:259]))
            self.assertTrue(all(value == 0 for value in samples[259:]))

    def test_wav_to_js_other_durations(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source.wav"
            samples = [-32768, -12345, -1, 0, 1, 12345, 32767] * 47
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))
            for frame_ms, frames in [(16, 1), (20, 1), (10, 2)]:
                socket = Socket(acknowledge=True)
                reference.read_wav(socket, path, channel="example", frame_ms=frame_ms, frames_per_packet=frames)
                request = json.loads(socket.sent[0])
                self.assertEqual(request["packet_duration"], frame_ms * frames)
                wire = dict(start={**request, "command": "on_stream_start", "stream_id": 42},
                            packets=[base64.b64encode(packet).decode() for packet in socket.sent[1:-1]],
                            stop=dict(command="on_stream_stop", stream_id=42))
                decoded = js(["--decode"], wire)
                self.assertEqual(decoded[:len(samples)], samples)
                self.assertTrue(all(sample == 0 for sample in decoded[len(samples):]))
                self.assertEqual(json.loads(socket.sent[-1])["command"], "stop_stream")

    def test_disconnect_and_bad_packets(self):
        wire = js(["--emit"])
        for messages in [[json.dumps(wire["request"])], [json.dumps(wire["request"]), b"bad"]]:
            target = Socket()
            with self.assertRaises((EOFError, ValueError)):
                reference.relay(Socket(messages), [target], stream_id=42, sender="alice")
            self.assertEqual(json.loads(target.sent[-1])["command"], "on_stream_stop")
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "partial.wav"
            with self.assertRaises(EOFError):
                reference.write_wav(Socket([json.dumps(wire["start"]), base64.b64decode(wire["packets"][0])]), path)
            with wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.getnframes(), 256)  # header finalized despite disconnect
        with self.assertRaises(ValueError):
            reference._format({**wire["start"], "codec": "opus"})
        with self.assertRaises(ValueError):
            reference._format({**wire["start"], "codec": "audio/L16"})
        with self.assertRaises(ValueError):
            reference._format({**wire["start"], "packet_duration": 20})

    def test_native_little_endian_passes_buffers_through(self):
        data = memoryview(b"\x01\x02\x03\x04")
        with patch.object(reference.sys, "byteorder", "little"):
            self.assertIs(reference._native_le16(data), data)
        with patch.object(reference.sys, "byteorder", "big"):
            self.assertEqual(reference._native_le16(data), b"\x02\x01\x04\x03")

    def test_relay_cleanup_reaches_other_receivers(self):
        wire = js(["--emit"])

        class ClosedReceiver(Socket):
            def send(self, message):
                if isinstance(message, str) and json.loads(message)["command"] == "on_stream_stop":
                    raise OSError("receiver closed")
                super().send(message)

        healthy = Socket()
        # Preserve the original malformed-packet error while attempting every stop send.
        with self.assertRaisesRegex(ValueError, "packet length"):
            reference.relay(Socket([json.dumps(wire["request"]), b"bad"]),
                            [ClosedReceiver(), healthy], stream_id=42, sender="alice")
        self.assertEqual(json.loads(healthy.sent[-1])["command"], "on_stream_stop")

    def test_threaded_wav_relay_round_trip(self):
        # Blocking peer sockets carry real messages between three worker threads.
        class Peer:
            def __init__(self):
                self.messages = queue.Queue()

            def recv(self):
                return self.messages.get(timeout=3)

            def send(self, message):
                if not isinstance(message, (str, bytes)):
                    message = b"".join(message)
                self.peer.messages.put(message)

        publisher, upstream, downstream, listener = (Peer() for _ in range(4))
        publisher.peer, upstream.peer = upstream, publisher
        downstream.peer, listener.peer = listener, downstream
        errors, results = [], []

        def worker(fn, *args, **kwargs):
            try:
                results.append(fn(*args, **kwargs))
            except Exception as error:
                errors.append(error)

        with tempfile.TemporaryDirectory() as scratch:
            source, target = Path(scratch) / "source.wav", Path(scratch) / "received.wav"
            samples = struct.pack("<4h", -32768, -1, 1, 32767) * 128
            with wave.open(str(source), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(samples)
            threads = [
                threading.Thread(target=worker, args=(reference.write_wav, listener, target)),
                threading.Thread(target=worker, args=(reference.relay, upstream, [downstream]),
                                 kwargs=dict(stream_id=42, sender="alice")),
                threading.Thread(target=worker, args=(reference.read_wav, publisher, source),
                                 kwargs=dict(channel="example", stop=threading.Event())),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(512, results)
            with wave.open(str(target), "rb") as audio:
                self.assertEqual(audio.readframes(512), samples)

    def test_wav_sender_stop_event(self):
        stop = threading.Event()
        socket = Socket(acknowledge=True)
        original_send = socket.send

        def send(message):
            original_send(message)
            if not isinstance(message, str):
                stop.set()  # first packet interrupts pacing and prevents a second packet

        socket.send = send
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x00\x01" * 1024)
            reference.read_wav(socket, path, channel="example", stop=stop)
            self.assertEqual(len(socket.sent), 3)  # start, one packet, stop
            self.assertEqual(json.loads(socket.sent[-1])["command"], "stop_stream")
            untouched = Socket()
            reference.read_wav(untouched, path, channel="example", stop=stop)
            self.assertEqual(untouched.sent, [])


if __name__ == "__main__":
    unittest.main()
