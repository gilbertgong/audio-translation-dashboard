"""JSON wire contracts, independent of the app, servers and inference."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import szal16


class MessageTests(unittest.TestCase):
    def test_command_acknowledgments(self):
        self.assertEqual(szal16.success_response(1), {"seq": 1, "success": True})
        self.assertEqual(szal16.success_response(2, 0), {"seq": 2, "success": True, "stream_id": 0})

    def test_echo_and_recorded_stream_start(self):
        for codec in ("pcm_s16le", "opus"):
            with self.subTest(codec=codec):
                args = dict(channel="test", stream_id=42, codec=codec, codec_header="gD4BEA==",
                            packet_duration=16, sender="echo")
                expected = {"command": "on_stream_start", "channel": "test", "stream_id": 42,
                            "type": "audio", "codec": codec, "codec_header": "gD4BEA==",
                            "packet_duration": 16, "from": "echo"}
                self.assertEqual(szal16.on_stream_start(**args), expected)
                self.assertEqual(szal16.on_stream_start(**args, message_id=0), {**expected, "message_id": 0})

    def test_stream_stop(self):
        self.assertEqual(szal16.on_stream_stop(42), {"command": "on_stream_stop", "stream_id": 42})

    def test_transcription_and_translation(self):
        translated = szal16.translation("en", "Hello")
        self.assertEqual(translated, {"language": "en", "message": "Hello"})
        result = szal16.on_transcription(stream_id=42, text="你好", language="zh",
                                         translations=[translated])
        self.assertEqual(result, {"command": "on_transcription", "stream_id": 42,
                                  "text": "你好", "language": "zh",
                                  "translations": [{"language": "en", "message": "Hello"}]})
        self.assertIn("你好", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("message_id", result)
        self.assertNotIn("seq", result)

    def test_optional_timing_extensions_preserve_values(self):
        args = dict(stream_id=42, text="hello", language="en", translations=[])
        base = szal16.on_transcription(**args)
        self.assertNotIn("stt_seconds", base)
        self.assertNotIn("llm_seconds", base)
        for timings in ({"stt_seconds": 0}, {"llm_seconds": 0},
                        {"stt_seconds": 0.81, "llm_seconds": 0.3130000000237487}):
            with self.subTest(timings=timings):
                self.assertEqual(szal16.on_transcription(**args, **timings), {**base, **timings})

    def test_error_and_optional_correlation_extensions(self):
        self.assertEqual(szal16.on_error("failure"), {"command": "on_error", "error": "failure"})
        self.assertEqual(szal16.on_error("语音失败", message_id=7, stream_id=42),
                         {"command": "on_error", "message_id": 7, "stream_id": 42, "error": "语音失败"})
        self.assertEqual(szal16.on_error("failure", message_id=0, stream_id=0),
                         {"command": "on_error", "message_id": 0, "stream_id": 0, "error": "failure"})


if __name__ == "__main__":
    unittest.main()
