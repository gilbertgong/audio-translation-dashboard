"""Voice URL resolution and Qwen upload checks; no model/server processes."""

import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import common
import run_servers


class VoiceURLTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / "tmp"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="voice-url-", dir=work)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {"_config_dir": self.root}

    def test_local_paths_do_not_download(self):
        with patch.object(common, "download") as fetch:
            self.assertEqual(common.voice_file(self.cfg, "voices/local.wav"), self.root / "voices/local.wav")
            absolute = self.root / "absolute.wav"
            self.assertEqual(common.voice_file(self.cfg, str(absolute)), absolute)
            fetch.assert_not_called()

    def test_urls_cache_once_and_use_distinct_paths_for_distinct_urls(self):
        def write(url, path):
            path.write_bytes(b"WAV fixture")
        with patch.object(common, "download", side_effect=write) as fetch:
            first = common.voice_file(self.cfg, "https://example.test/reference.wav?v=1")
            self.assertTrue(first.is_relative_to(self.root / "voices/cache"))
            self.assertEqual(first.name, "reference.wav")
            self.assertEqual(common.voice_file(self.cfg, "https://example.test/reference.wav?v=1"), first)
            fetch.assert_called_once()
            second = common.voice_file(self.cfg, "http://example.test/reference.wav?v=2")
            self.assertNotEqual(first, second)
            self.assertEqual(fetch.call_count, 2)

    def test_failed_download_is_retried(self):
        with patch.object(common, "download", side_effect=OSError("connection lost")):
            with self.assertRaisesRegex(OSError, "connection lost"):
                common.voice_file(self.cfg, "https://example.test/reference.wav")
        self.assertFalse(list(self.root.rglob("*.wav")))
        with patch.object(common, "download", side_effect=lambda url, path: path.write_bytes(b"wav")) as fetch:
            self.assertTrue(common.voice_file(self.cfg, "https://example.test/reference.wav").is_file())
            fetch.assert_called_once()

    def test_qwen_uploads_cached_bytes_through_existing_base64_api(self):
        wav = b"RIFF-test-reference-WAVE"
        server = {"host": "127.0.0.1", "port": 9999,
                  "voices": {"demo": "https://example.test/reference.wav"}}
        with patch.object(common, "download", side_effect=lambda url, path: path.write_bytes(wav)) as fetch, \
                patch.object(run_servers.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")) as upload:
            run_servers.register_voices(self.cfg, "qwentts", server, Mock())
            body = json.loads(upload.call_args.args[0].data)
            self.assertEqual(body["name"], "demo")
            self.assertEqual(base64.b64decode(body["wav_b64"]), wav)
            self.assertEqual(upload.call_args.args[0].full_url, "http://127.0.0.1:9999/v1/audio/voices")
            fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
