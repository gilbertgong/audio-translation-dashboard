"""Offline parity with the pinned reference pipeline, without PyTorch or models."""

import ast
import dataclasses
import io
import json
import re
import sys
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import configuration
import app
import kokoro_server as server
import run_servers


def english(text):
    tokens = []
    for match in re.finditer(r"(\w+|[^\w\s])(\s*)", text):
        tokens.append(types.SimpleNamespace(text=match[1], phonemes=match[1], whitespace=match[2]))
    return text, tokens


def fake_misaki():
    en = types.SimpleNamespace(G2P=Mock(side_effect=lambda **kw: english))
    espeak = types.SimpleNamespace(EspeakFallback=Mock(return_value="fallback"),
                                   EspeakG2P=Mock(side_effect=lambda **kw: lambda text: (text, None)))
    ja = types.SimpleNamespace(JAG2P=Mock(return_value=lambda text: (text, None)))
    zh = types.SimpleNamespace(ZHG2P=Mock(return_value=lambda text: (text, None)))
    return types.SimpleNamespace(en=en, espeak=espeak, ja=ja, zh=zh)


def load_reference(misaki):
    # Execute only KPipeline's class, with model=False and lightweight stand-ins.
    # Its original source is preserved with whitespace normalization in tests/reference for comparison.
    source = (ROOT / "tests/reference/kokoro_pipeline.py.txt").read_text(encoding="utf-8")
    cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "KPipeline")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[future, cls], type_ignores=[]))
    module = types.ModuleType("kokoro_reference")
    module.__dict__.update(KModel=type("KModel", (), {}), dataclass=dataclasses.dataclass,
                          en=misaki.en, espeak=misaki.espeak, logger=Mock(), re=re,
                          torch=types.SimpleNamespace(FloatTensor=type("FloatTensor", (), {})),
                          ALIASES=server.LANG_ALIASES, LANG_CODES=server.LANG_CODES)
    sys.modules[module.__name__] = module
    exec(compile(tree, "kokoro_reference", "exec"), module.__dict__)
    return module.KPipeline


class KokoroPipelineTests(unittest.TestCase):
    def setUp(self):
        self.misaki = fake_misaki()
        self.engine = server.Kokoro.__new__(server.Kokoro)
        self.engine.repo_id = "hexgrad/Kokoro-82M-v1.1-zh"
        self.engine.misaki = self.misaki
        self.engine.g2ps = {}
        self.reference = load_reference(self.misaki)
        module = types.ModuleType("misaki")
        module.ja, module.zh = self.misaki.ja, self.misaki.zh
        self.modules = patch.dict(sys.modules, {"misaki": module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_chunk_outputs_match_reference(self):
        examples = ["", "Hello, world!", "  First line.\n\nSecond line.  ",
                    "word " * 400, "x" * 700, "hello. " * 100,
                    "你好。" * 180, "こんにちは！" * 120,
                    "x" * 395 + "." + "second sentence? " * 40,
                    "Bonjour. " * 100, "Hello 你好 Japanese こんにちは."]
        for code in server.LANG_CODES:
            pipeline = self.reference(code, repo_id=self.engine.repo_id, model=False,
                                      en_callable=lambda text: english(text)[0])
            for text in examples:
                with self.subTest(code=code, text=text[:25]):
                    expected = [result.phonemes for result in pipeline(text)]
                    self.assertEqual(list(self.engine.chunks(text, code)), expected)

    def test_model_package_selects_reference_chinese_frontend(self):
        for repo, expected in (("hexgrad/Kokoro-82M", None), ("hexgrad/Kokoro-82M-v1.1-zh", "1.1")):
            self.engine.repo_id = repo
            self.engine.g2ps.clear()
            self.engine.phonemizer("z")
            actual = self.misaki.zh.ZHG2P.call_args.kwargs["version"]
            self.reference("z", repo_id=repo, model=False)
            reference = self.misaki.zh.ZHG2P.call_args.kwargs["version"]
            self.assertEqual(actual, reference)
            self.assertEqual(actual, expected)

    def test_british_pipeline_and_other_languages_use_reference_settings(self):
        self.engine.phonemizer("b")
        self.assertTrue(self.misaki.en.G2P.call_args.kwargs["british"])
        self.assertTrue(self.misaki.espeak.EspeakFallback.call_args.kwargs["british"])
        for code in "efhip":
            self.engine.phonemizer(code)
            self.misaki.espeak.EspeakG2P.assert_called_with(language=server.LANG_CODES[code])

    def test_english_fallback_failure_matches_reference(self):
        self.misaki.espeak.EspeakFallback.side_effect = RuntimeError("not installed")
        with self.assertLogs(server.log, level="WARNING"):
            self.engine.phonemizer("a")
        self.assertIsNone(self.misaki.en.G2P.call_args.kwargs["fallback"])
        self.reference("a", repo_id=self.engine.repo_id, model=False)
        self.assertIsNone(self.misaki.en.G2P.call_args.kwargs["fallback"])

    def test_legacy_script_routing_remains_available(self):
        for text, code in (("Hello", "a"), ("你好", "z"), ("こんにちは", "j")):
            self.engine.g2ps.clear()
            list(self.engine.chunks(text))
            self.assertIn(code, self.engine.g2ps)

    def test_voice_blend_matches_reference_average(self):
        average = lambda packs, **kw: [sum(values) / len(values) for values in zip(*packs)]
        self.engine.np = types.SimpleNamespace(stack=lambda packs: packs, mean=average)
        self.engine.packs = {"af_one": [1, 3], "af_two": [3, 5]}
        self.engine.voices = list(self.engine.packs)
        pipeline = self.reference("a", repo_id=self.engine.repo_id, model=False)
        pipeline.voices = dict(self.engine.packs)
        reference_module = sys.modules["kokoro_reference"]
        reference_module.torch.stack = lambda packs: packs
        reference_module.torch.mean = average
        self.assertEqual(self.engine.voice_pack("af_one,af_two"), pipeline.load_voice("af_one,af_two"))
        self.assertIsNone(self.engine.voice_pack("af_one,missing"))

    def test_fragments_merge_with_correct_model_and_voices(self):
        defaults = tomllib.loads((ROOT / "config/defaults.toml").read_text(encoding="utf-8"))
        for version, repo in (("1.0", "hexgrad/Kokoro-82M"), ("1.1", "hexgrad/Kokoro-82M-v1.1-zh")):
            fragment = tomllib.loads((ROOT / f"config/fragments/kokoro_v{version}.toml").read_text(encoding="utf-8"))
            cfg = configuration.merge(defaults, fragment)
            cfg["_config_dir"] = ROOT
            engine = cfg["api_servers"]["kokoro"]
            self.assertEqual(engine["args"][-2:], ["--repo-id", repo])
            if version == "1.0":
                self.assertEqual(run_servers.voice_files(cfg, engine), {})
                self.assertEqual(engine["language_voices"], {"english": "af_heart", "chinese": "zf_xiaoxiao", "japanese": "jf_alpha"})
            else:
                self.assertIn("beth_thomas", run_servers.voice_files(cfg, engine))

    def test_http_adapter_passes_reference_language_alias(self):
        engine = Mock(voices=["af_heart"])
        engine.generate.side_effect = lambda text, pack, speed, callback, code: callback(bytes(4))
        for fmt in ("wav", "pcm"):
            request = Mock()
            request.json.return_value = {"input": "Hello", "voice": "af_heart", "lang_code": "en-GB", "response_format": fmt}
            with patch.object(server, "tts", engine):
                self.assertTrue(list(server.speech(request)))
            self.assertEqual(engine.generate.call_args.args[-1], "b")

    def test_dashboard_supplies_known_language_only_to_kokoro(self):
        for engine_name in ("kokoro", "qwentts"):
            cfg = {"tts": {"server": engine_name}, "tts_url": "http://mock",
                   "languages": {"language1": "English", "language2": "Japanese"}}
            request = Mock()
            request.json.return_value = {"text": "東京駅", "target": "language2"}
            with patch.object(app, "config", cfg), patch.object(app, "mock", False), \
                 patch.object(app, "choose_voice", return_value="voice"), \
                 patch.object(app, "open_server", return_value=io.BytesIO(bytes(4))) as outgoing:
                self.assertTrue(list(app.post_speak(request)))
                body = json.loads(outgoing.call_args.args[1])
            self.assertEqual(body.get("lang_code"), "j" if engine_name == "kokoro" else None)


if __name__ == "__main__":
    unittest.main()
