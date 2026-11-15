"""Channel pipeline checks with real local HTTP/WS and stubbed inference only."""
import http.client
from contextlib import closing
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch
import wave

import test_echo
from test_echo import connection, send, receive, app
from microweb import Stream


class ChannelTests(test_echo.EchoTests):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "config/defaults.toml").read_text(encoding="utf-8"))
        config["audio"]["temp_dir"] = Path(self.folder.name)
        config.update(unknown_languages=[], whisper_url="http://mock", llama_url="http://mock", tts_url="http://mock")
        for name, value in [("config", config), ("mock", False)]:
            change = patch.object(app, name, value)
            change.start(); self.addCleanup(change.stop)
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(3200))
        self.wav = data.getvalue()

    def post(self, channel="one", seq="1"):
        with closing(http.client.HTTPConnection(*self.server.server_address, timeout=3)) as client:
            client.request("POST", f"/api/channel/translate?channel={channel}&seq={seq}", self.wav,
                           {"Content-Type": "audio/wav"})
            reply = client.getresponse()
            return reply.status, json.loads(reply.read())

    def transcript(self, language="english", text="hello"):
        return dict(text=text, language=language, confidence=-0.1, stt_raw={"source": "test"})

    def test_post_completes_while_speech_runs_and_next_text_is_not_blocked(self):
        started, release = threading.Event(), threading.Event()
        pcm = struct.pack("<h", 12345) * 400

        def chunks():
            started.set()
            release.wait(3)
            yield pcm[:1]
            yield pcm[1:795]
            yield pcm[795:]

        with patch.object(app, "transcribe", return_value=self.transcript()), \
             patch.object(app, "call_server", return_value={"choices": [{"message": {"content": "你好"}}]}) as llama, \
             patch.object(app, "speech_stream", side_effect=lambda *args: Stream(chunks(), app.TTS_PCM_TYPE)) as tts:
            try:
                with connection(self.server, "channel=one&echo=0") as sock:
                    mic_id = self.start(sock, "one", echo=False)
                    send(sock, struct.pack("!BII", 1, mic_id, 0) + bytes(1536))
                    self.assertEqual(self.post(), (200, {"seq": "1"}))
                    self.assertEqual(receive(sock)["command"], "on_transcription")
                    translated = receive(sock)
                    self.assertEqual(translated["result"]["translation"], "你好")
                    self.assertEqual(translated["result"]["raw"]["response"]["choices"][0]["message"]["content"], "你好")
                    first = receive(sock)
                    self.assertEqual(first["command"], "on_stream_start")
                    self.assertTrue(started.wait(1))
                    # While first speech is blocked, POST #2 still completes and delivers text.
                    self.assertEqual(self.post(seq="2"), (200, {"seq": "2"}))
                    self.assertEqual(receive(sock)["seq"], "2")
                    self.assertEqual(receive(sock)["seq"], "2")
                    self.assertEqual(tts.call_count, 1)  # second speech waits in order
                    body = json.loads(llama.call_args.args[1])
                    self.assertIs(body["stream"], False)
                    self.assertNotIn("stream_options", body)
                    release.set()
                    for index in range(2):
                        event = first if index == 0 else receive(sock)
                        self.assertEqual(event["command"], "on_stream_start")
                        packets = [receive(sock), receive(sock)]
                        self.assertEqual([struct.unpack_from("!I", packet, 5)[0] for packet in packets], [0, 1])
                        self.assertEqual(packets[0][9:] + packets[1][9:], pcm.ljust(1536, b"\0"))
                        self.assertEqual(receive(sock)["command"], "on_stream_stop")
            finally:
                release.set()

    def test_recheck_direction_and_empty_transcript(self):
        checked = dict(text="你好", language="chinese", confidence=-0.01, stt_seconds=0.2, stt_raw={"attempts": []})
        with patch.object(app, "transcribe", return_value=self.transcript("russian")), \
             patch.object(app, "recheck_recording", return_value=checked) as recheck, \
             patch.object(app, "call_server", return_value={"choices": [{"message": {"content": "hello"}}]}), \
             patch.object(app, "speech_stream", return_value=Stream([], app.TTS_PCM_TYPE)) as tts:
            with connection(self.server, "channel=one&echo=0") as sock:
                self.start(sock, "one", echo=False)
                self.assertEqual(self.post()[0], 200)
                result = receive(sock)["result"]
                self.assertEqual(result["language"], "chinese")
                self.assertEqual(result["recheck_result"], checked)
                self.assertEqual(recheck.call_args.args[0], self.wav)
                self.assertEqual(receive(sock)["command"], "on_translation")
                self.assertEqual(receive(sock)["command"], "on_stream_start")
                self.assertEqual(receive(sock)["command"], "on_stream_stop")
                self.assertEqual(tts.call_args.args, ("hello", "language1"))
        with patch.object(app, "transcribe", return_value=self.transcript(text="")), patch.object(app, "call_server") as llama:
            with connection(self.server, "channel=two&echo=0") as sock:
                self.start(sock, "two", echo=False)
                self.assertEqual(self.post(channel="two")[0], 200)
                self.assertEqual(receive(sock)["result"]["text"], "")
                llama.assert_not_called()

    def test_disconnect_discards_pending_inference_and_does_not_target_reconnect(self):
        started, release = threading.Event(), threading.Event()

        def transcribe(*args, **kwargs):
            started.set(); release.wait(3)
            return self.transcript()

        outcome = []
        with patch.object(app, "transcribe", side_effect=transcribe), patch.object(app, "call_server") as llama:
            with connection(self.server, "channel=one&echo=0") as sock:
                self.start(sock, "one", echo=False)
                job = threading.Thread(target=lambda: outcome.append(self.post()))
                job.start(); self.assertTrue(started.wait(1))
                sock.close()
                deadline = time.monotonic() + 2
                while "one" in app.audio_channels and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertNotIn("one", app.audio_channels)
                with connection(self.server, "channel=one&echo=0") as replacement:
                    self.start(replacement, "one", echo=False)
                    release.set(); job.join(3)
                    self.assertFalse(job.is_alive())
                    self.assertEqual(outcome[0][0], 410)
                    llama.assert_not_called()
                    replacement.settimeout(0.05)
                    with self.assertRaises(TimeoutError):
                        receive(replacement)

    def test_disconnect_discards_queued_speech(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def chunks():
            try:
                started.set(); release.wait(3)
                yield bytes(768)
            finally:
                finished.set()

        with patch.object(app, "transcribe", return_value=self.transcript()), \
             patch.object(app, "call_server", return_value={"choices": [{"message": {"content": "你好"}}]}), \
             patch.object(app, "speech_stream", side_effect=lambda *args: Stream(chunks(), app.TTS_PCM_TYPE)) as tts:
            try:
                with connection(self.server, "channel=one&echo=0") as sock:
                    self.start(sock, "one", echo=False)
                    self.assertEqual(self.post()[0], 200)
                    self.assertTrue(started.wait(1))
                    self.assertEqual(self.post(seq="2")[0], 200)
                    with app.audio_channels_lock:
                        session = app.audio_channels["one"]
                    sock.close()
                    self.assertTrue(session["stop"].wait(1))
                    release.set()
                    self.assertTrue(finished.wait(1))
                    self.assertEqual(tts.call_count, 1)
            finally:
                release.set()

    def test_mock_pipeline_and_speech_error(self):
        with patch.object(app, "mock", True), patch.object(app, "MOCK_DELAY", 0), \
             patch.object(app, "mock_speech_stream", return_value=iter([bytes(768)])):
            with connection(self.server, "channel=one&echo=0") as sock:
                self.start(sock, "one", echo=False)
                self.assertEqual(self.post()[0], 200)
                self.assertEqual(receive(sock)["command"], "on_transcription")
                self.assertTrue(receive(sock)["result"]["raw"]["response"]["mock"])
                self.assertEqual(receive(sock)["command"], "on_stream_start")
                self.assertEqual(len(receive(sock)), 777)
                self.assertEqual(receive(sock)["command"], "on_stream_stop")
        with patch.object(app, "transcribe", return_value=self.transcript()), \
             patch.object(app, "call_server", return_value={"choices": [{"message": {"content": "你好"}}]}), \
             patch.object(app, "speech_stream", side_effect=app.HTTPError(502, "speech failed")):
            with connection(self.server, "channel=two&echo=0") as sock:
                self.start(sock, "two", echo=False)
                self.assertEqual(self.post(channel="two")[0], 200)
                receive(sock); receive(sock)
                error = receive(sock)
                self.assertEqual(error["command"], "on_speech_error")
                self.assertEqual(error["error"], "speech failed")


if __name__ == "__main__":
    unittest.main()
