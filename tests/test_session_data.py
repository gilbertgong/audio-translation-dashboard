"""Offline temporary recording and application logging checks."""

import io
import json
import tempfile
import time
import tomllib
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import app
from microweb import HTTPError

WAV = b"RIFF" + bytes(4) + b"WAVE" + bytes(32)
SECRET = "private conversation sentinel"


def transcript(language="english", confidence=-0.1):
    return {"text": SECRET, "language": language, "confidence": confidence, "stt_raw": {"text": SECRET}}


class SessionDataTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="session-test-", dir=ROOT / "tmp")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = tomllib.loads((ROOT / "config/defaults.toml").read_text(encoding="utf-8"))
        self.cfg["audio"]["temp_dir"] = self.root
        self.cfg.update(unknown_languages=[], whisper_url="http://mock", llama_url="http://mock", tts_url="http://mock")
        self.config_patch = patch.object(app, "config", self.cfg)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.mock_patch = patch.object(app, "mock", False)
        self.mock_patch.start()
        self.addCleanup(self.mock_patch.stop)
        self.addCleanup(app.close_temporary_recordings)
        self.files = []
        original = tempfile.TemporaryFile

        def track(*args, **kwargs):
            recording = original(*args, **kwargs)
            self.files.append(recording)
            return recording

        temporary_patch = patch.object(app.tempfile, "TemporaryFile", side_effect=track)
        temporary_patch.start()
        self.addCleanup(temporary_patch.stop)

    def upload(self, result=None):
        request = Mock()
        request.body.return_value = WAV
        with patch.object(app, "transcribe", return_value=result or transcript()):
            payload, status = app.post_recording(request)
        self.assertEqual(status, 201)
        self.assertEqual(payload["text"], SECRET)
        return payload

    def recheck(self, payload, results):
        request = Mock()
        request.json.return_value = {"file": Path(payload["file"]).name}
        with patch.object(app, "transcribe", side_effect=results) as transcribe:
            result = app.post_recheck(request)
            self.assertEqual([call.args[1] for call in transcribe.call_args_list], [WAV, WAV])
            self.assertEqual([call.kwargs["language"] for call in transcribe.call_args_list], ["english", "chinese"])
        return result

    def test_default_closes_file_without_recheck_and_keeps_activity_logs(self):
        with self.assertLogs("app", level="INFO") as logs:
            payload = self.upload()
        self.assertTrue(payload["temporary"])
        self.assertFalse(payload["recheck"])
        self.assertTrue(self.files[0].closed)
        self.assertFalse(list(self.root.iterdir()))
        self.assertFalse(app.temporary_recordings)
        output = "\n".join(logs.output)
        self.assertIn("transcribed", output)
        self.assertIn("english", output)
        self.assertIn("[content omitted]", output)
        self.assertNotIn(SECRET, output)

    def test_recheck_uses_open_server_file_then_closes_it(self):
        payload = self.upload(transcript("russian"))
        self.assertTrue(payload["recheck"])
        self.assertFalse(self.files[0].closed)
        with self.assertLogs("app", level="INFO") as logs:
            result = self.recheck(payload, [transcript("english", -1), transcript("chinese", -0.1)])
        self.assertEqual(result["language"], "chinese")
        self.assertTrue(self.files[0].closed)
        self.assertFalse(list(self.root.iterdir()))
        self.assertFalse(app.temporary_recordings)
        self.assertIn("confidence", "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_failed_initial_transcription_closes_file(self):
        request = Mock()
        request.body.return_value = WAV
        with patch.object(app, "transcribe", side_effect=OSError("connection refused")):
            with self.assertRaises(HTTPError), self.assertLogs("app", level="WARNING") as logs:
                app.post_recording(request)
        self.assertTrue(self.files[0].closed)
        self.assertFalse(list(self.root.iterdir()))
        self.assertIn("connection refused", "\n".join(logs.output))

    def test_failed_recheck_closes_file(self):
        payload = self.upload(transcript("russian"))
        with self.assertRaises(HTTPError), self.assertLogs("app", level="WARNING"):
            self.recheck(payload, [OSError("offline"), OSError("offline")])
        self.assertTrue(self.files[0].closed)
        self.assertFalse(app.temporary_recordings)
        self.assertFalse(list(self.root.iterdir()))

    def test_expiry_closes_abandoned_file(self):
        self.upload(transcript("russian"))
        timer = next(iter(app.temporary_recordings.values()))[1]
        timer.function(*timer.args)  # execute the expiry callback without a real wait
        self.assertTrue(self.files[0].closed)
        self.assertFalse(app.temporary_recordings)
        self.assertFalse(list(self.root.iterdir()))

    def test_shutdown_closes_pending_files(self):
        self.upload(transcript("russian"))
        app.close_temporary_recordings()
        self.assertTrue(self.files[0].closed)
        self.assertFalse(list(self.root.iterdir()))

    def test_opt_in_restores_saved_files_and_content(self):
        self.cfg["diagnostics"]["save_session_data"] = True
        with self.assertLogs("app", level="INFO") as logs:
            payload = self.upload(transcript("russian"))
            self.recheck(payload, [transcript("english"), transcript("chinese", -1)])
        self.assertFalse(payload["temporary"])
        self.assertEqual(Path(payload["file"]).read_bytes(), WAV)
        self.assertFalse(self.files)
        self.assertIn(SECRET, "\n".join(logs.output))

    def test_translation_log_preserves_metrics_without_content(self):
        line = b"data: " + json.dumps({"choices": [{"delta": {"content": SECRET}}]}).encode() + b"\n"
        with self.assertLogs("app", level="INFO") as logs:
            result = list(app.relay_translation(SECRET, "http://mock", {}, io.BytesIO(line), time.monotonic()))
        self.assertIn(line, result)  # normal response remains unchanged
        self.assertIn("first token", "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_speech_log_preserves_metrics_without_translation_text(self):
        request = Mock()
        request.json.return_value = {"text": SECRET}
        with patch.object(app, "open_server", return_value=io.BytesIO(bytes(4800))):
            with self.assertLogs("app", level="INFO") as logs:
                self.assertTrue(list(app.post_speak(request)))
        self.assertIn("spoke", "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_translation_failure_keeps_diagnostic_but_omits_input_field(self):
        request = Mock()
        request.json.return_value = {"text": SECRET, "source": "English"}
        with patch.object(app, "open_server", side_effect=OSError("connection refused")):
            with self.assertRaises(HTTPError), self.assertLogs("app", level="WARNING") as logs:
                app.post_translate(request)
        self.assertIn("connection refused", "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
