"""Offline cross-language stream/WAV checks: python tests/test_szaL16.py."""

import base64
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
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

    async def recv(self):
        if not self.messages:
            raise EOFError("disconnected")
        return self.messages.pop(0)

    async def send(self, message):
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


class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_js_to_relay_to_wav(self):
        wire = js(["--emit"])
        packets = [base64.b64decode(packet) for packet in wire["packets"]]
        outgoing = [json.dumps(wire["request"]), *packets,
                    json.dumps(dict(command="stop_stream", stream_id=42))]
        source, first, second = Socket(outgoing), Socket(), Socket()
        await reference.relay(source, [first, second], stream_id=42, sender="alice")
        self.assertEqual(first.sent, second.sent)
        self.assertEqual(json.loads(source.sent[0]), dict(seq=7, success=True, stream_id=42))
        self.assertEqual(json.loads(first.sent[0])["from"], "alice")
        self.assertEqual(struct.unpack_from("!I", first.sent[2], 5)[0], 1)
        # The relay passes a view of the original payload, not a converted/copy buffer.
        self.assertIs(first.fragments[0][1].obj, packets[0])
        self.assertEqual(first.sent[1][9:], packets[0][9:])
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "received.wav"
            count = await reference.write_wav(Socket(first.sent), path)
            self.assertEqual(count, 512)
            with wave.open(str(path), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 16000))
                samples = struct.unpack("<512h", audio.readframes(512))
            self.assertEqual(samples[:9], (-32768, -16384, 0, 16384, 32767, -32768, 32767, 1, 32767))
            self.assertTrue(all(value == 8192 for value in samples[9:259]))
            self.assertTrue(all(value == 0 for value in samples[259:]))

    async def test_wav_to_js_other_durations(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source.wav"
            samples = [-32768, -12345, -1, 0, 1, 12345, 32767] * 47
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))
            for frame_ms, frames in [(16, 1), (20, 1), (10, 2)]:
                socket = Socket(acknowledge=True)
                await reference.read_wav(socket, path, channel="example", frame_ms=frame_ms, frames_per_packet=frames)
                request = json.loads(socket.sent[0])
                self.assertEqual(request["packet_duration"], frame_ms * frames)
                wire = dict(start={**request, "command": "on_stream_start", "stream_id": 42},
                            packets=[base64.b64encode(packet).decode() for packet in socket.sent[1:-1]],
                            stop=dict(command="on_stream_stop", stream_id=42))
                decoded = js(["--decode"], wire)
                self.assertEqual(decoded[:len(samples)], samples)
                self.assertTrue(all(sample == 0 for sample in decoded[len(samples):]))
                self.assertEqual(json.loads(socket.sent[-1])["command"], "stop_stream")

    async def test_disconnect_and_bad_packets(self):
        wire = js(["--emit"])
        for messages in [[json.dumps(wire["request"])], [json.dumps(wire["request"]), b"bad"]]:
            target = Socket()
            with self.assertRaises((EOFError, ValueError)):
                await reference.relay(Socket(messages), [target], stream_id=42, sender="alice")
            self.assertEqual(json.loads(target.sent[-1])["command"], "on_stream_stop")
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "partial.wav"
            with self.assertRaises(EOFError):
                await reference.write_wav(Socket([json.dumps(wire["start"]), base64.b64decode(wire["packets"][0])]), path)
            with wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.getnframes(), 256)  # header finalized despite disconnect
        with self.assertRaises(ValueError):
            reference._format({**wire["start"], "codec": "opus"})
        with self.assertRaises(ValueError):
            reference._format({**wire["start"], "packet_duration": 20})


if __name__ == "__main__":
    unittest.main()
