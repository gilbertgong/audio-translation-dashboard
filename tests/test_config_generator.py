"""Offline generator checks; no user config files or model processes touched."""

import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("config_launcher", str(ROOT / "launcher.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class ConfigGeneratorTests(unittest.TestCase):
    maxDiff = None

    def test_choices_include_both_kokoro_packages_and_group_tts(self):
        _, groups, _ = launcher.backend_choices()
        self.assertEqual(set(groups), {"Transcription", "Translation", "Speech"})
        self.assertIn("kokoro_v1.0", groups["Speech"])
        self.assertIn("kokoro_v1.1", groups["Speech"])
        self.assertIn("Remote kokoro", groups["Speech"])
        self.assertIn("Remote qwentts", groups["Speech"])
        self.assertEqual(list(groups["Speech"])[-2:], ["Remote kokoro", "Remote qwentts"])

    def test_resource_estimates_for_cpu_small_context_and_codec(self):
        _, _, fragments = launcher.backend_choices()
        def resources(name):
            return launcher.fragment_resources(name, fragments[name])
        self.assertEqual(resources("kokoro_v1.0"), (0.33, 0))
        files, vram = resources("llama_qwen3.5-4b-ud-q4_k_xl")
        self.assertEqual(files, 2.91)
        self.assertAlmostEqual(vram, 2.91 * 1.1)
        files, vram = resources("llama_qwen3.8-27b-ud-iq3_xxs")
        self.assertAlmostEqual(vram, files * 1.2)
        files, vram = resources("qwentts_1.7b-base")
        self.assertEqual(files, 2.37)
        self.assertAlmostEqual(vram, 2.37 * 1.2)

    def test_missing_size_uses_only_complete_downloaded_model_files(self):
        work = ROOT / "tmp"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fragment-size-", dir=work) as scratch:
            folder = Path(scratch)
            fragment = folder / "custom.toml"
            fragment.write_text("# A fragment without size metadata\n", encoding="utf-8")
            data = {"api_servers": {"qwentts": {"start": True}},
                    "models": {"qwentts": {"model": "https://example.test/talker.gguf",
                                            "codec": "https://example.test/codec.gguf"}}}
            models = folder / "weights" / "qwentts"
            models.mkdir(parents=True)
            (models / "talker.gguf").write_bytes(b"a" * 1200)
            (models / "codec.gguf.part").write_bytes(b"b" * 300)
            with patch.object(launcher, "ROOT", folder), patch.object(launcher, "FRAGMENTS_DIR", folder), \
                    patch.object(launcher, "setting", return_value="weights"), \
                    patch("urllib.request.urlopen") as network:
                self.assertEqual(launcher.fragment_resources("custom", data), (None, None))
                (models / "codec.gguf").write_bytes(b"b" * 300)
                files, vram = launcher.fragment_resources("custom", data)
                self.assertAlmostEqual(files, 1500 / 1e9)
                self.assertAlmostEqual(vram, files * 1.2)
                # A comment takes precedence over the on-disk fallback.
                fragment.write_text("# Model files: 2.37 GB (GPU).\n", encoding="utf-8")
                self.assertEqual(launcher.fragment_resources("custom", data)[0], 2.37)
                fragment.write_text("# No size comment\n", encoding="utf-8")
                data = {"api_servers": {"kokoro": {"start": True, "script": "src/kokoro_server.py"}},
                        "models": {"kokoro": {"kokoro": "https://example.test/bundle.tar.bz2#bundle/model.onnx"}}}
                onnx = folder / "weights" / "kokoro" / "bundle" / "model.onnx"
                onnx.parent.mkdir(parents=True)
                onnx.write_bytes(b"c" * 2000)
                self.assertEqual(launcher.fragment_resources("custom", data), (2000 / 1e9, 0.0))
                data = {"api_servers": {"whisper": {"start": True}},
                        "models": {"whisper": {"model": "large-v3"}}}
                whisper = folder / "weights" / "whisper" / "ggml-large-v3.bin"
                whisper.parent.mkdir(parents=True)
                whisper.write_bytes(b"d" * 1000)
                self.assertEqual(launcher.fragment_resources("custom", data)[0], 1000 / 1e9)
                network.assert_not_called()

    def test_mixed_local_and_remote_uses_defaults_for_remote(self):
        text = launcher.assemble_advanced([
            ("local", "whisper", "whisper_large-v3", "", ""),
            ("remote", "llama", None, "desktop.example", "9002"),
            ("remote", "kokoro", None, "192.168.1.7", "8083"),
        ], ["English", "Japanese"], 1)
        generated = tomllib.loads(text)
        self.assertEqual(generated["api_servers"]["llama"],
                         {"start": False, "host": "desktop.example", "port": 9002})
        self.assertEqual(generated["tts"]["server"], "kokoro")
        self.assertFalse(generated["api_servers"]["qwentts"]["start"])
        self.assertNotIn("kokoro", generated["models"])
        self.assertEqual(generated["gpu"]["devices"], "1")
        defaults, _, _ = launcher.backend_choices()
        merged = launcher.configuration.merge(defaults, generated)
        self.assertEqual(merged["api_servers"]["kokoro"]["language_voices"],
                         defaults["api_servers"]["kokoro"]["language_voices"])

    def test_all_remote_and_off_omit_gpu_and_models(self):
        for speech in ("kokoro", "qwentts"):
            text = launcher.assemble_advanced([
                ("remote", "whisper", None, 'host"name', "8081"),
                ("off", None, None, "", ""),
                ("remote", speech, None, "127.0.0.1", "8085"),
            ], gpu=0)
            generated = tomllib.loads(text)
            self.assertNotIn("gpu", generated)
            self.assertNotIn("models", generated)
            self.assertEqual(generated["tts"]["server"], speech)
            self.assertEqual(generated["api_servers"]["whisper"]["host"], 'host"name')
            self.assertTrue(all(not server["start"] for server in generated["api_servers"].values()))

    def test_local_advanced_matches_each_local_recipe(self):
        recipes = launcher.load_recipes()
        _, groups, _ = launcher.backend_choices()
        for name, recipe in recipes.items():
            if "remote_host" in recipe:
                continue
            selections = []
            for choices in groups.values():
                selected = next((entry for entry in choices.values() if entry[2] in recipe["fragments"]),
                                ("off", None, None))
                selections.append(selected + ("", ""))
            self.assertEqual(tomllib.loads(launcher.assemble_advanced(selections, gpu=0)),
                             tomllib.loads(launcher.assemble(name, recipe, gpu=0)))

    def test_coloring_keeps_hash_in_strings(self):
        text = '[models.kokoro]\nurl = "https://example/model#file" # comment\nstart = false\nport = 8083\n'
        tokens = [(m.lastgroup, m.group()) for m in launcher.TOML_TOKENS.finditer(text)]
        self.assertIn(("string", '"https://example/model#file"'), tokens)
        self.assertIn(("comment", "# comment"), tokens)
        self.assertIn(("section", "[models.kokoro]"), tokens)
        self.assertIn(("literal", "false"), tokens)
        self.assertIn(("literal", "8083"), tokens)

    def test_widget_mode_switch_remote_fields_and_scratch_save(self):
        root = launcher.tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        callback = Mock()
        with patch.object(launcher.threading, "Thread"):
            window = launcher.GenerateConfigWindow(root, callback)
        window.window.withdraw()
        window.combo.current(list(window.recipes).index("nvidia-8gb"))
        window.show()
        window.advanced.set(True)
        window.toggle_advanced()
        self.assertEqual(window.backends["Speech"][0].get(), "kokoro_v1.1 (0.33 GB)")
        self.assertIn("6.92 GB", window.resource_totals.cget("text"))
        errors = []
        root.report_callback_exception = lambda *args: errors.append(str(args[1]))
        for label, expected in ((window.resource_totals, "Estimated VRAM adds"),
                                (window.backend_sizes["Speech"], "Runs on CPU")):
            label.event_generate("<Enter>")
            root.update_idletasks()
            popups = label.winfo_children()
            self.assertEqual(len(popups), 1)
            contents = popups[0].winfo_children()[0].winfo_children()[0].cget("text")
            self.assertIn(expected, contents)
            label.event_generate("<Leave>")
            self.assertFalse(label.winfo_children())
        self.assertEqual(errors, [])
        choice, host, port, remote = window.backends["Speech"]
        choice.set("Remote qwentts")
        window.select_backend("Speech")
        host.set("192.168.1.8")
        port.set("9005")
        self.assertEqual(remote.winfo_manager(), "pack")
        self.assertEqual(tomllib.loads(window.text)["api_servers"]["qwentts"]["port"], 9005)
        self.assertTrue(window.preview.tag_ranges("comment"))
        self.assertTrue(window.preview.tag_ranges("string"))
        window.advanced.set(False)
        window.toggle_advanced()
        self.assertEqual(window.recipe_row.winfo_manager(), "pack")
        window.combo.current(list(window.recipes).index("remote-nvidia-24gb"))
        window.remote_host.set("desktop.example")
        window.advanced.set(True)
        window.toggle_advanced()
        self.assertEqual(choice.get(), "Remote qwentts")
        self.assertTrue(all(var[1].get() == "desktop.example" for var in window.backends.values()))
        self.assertNotIn("gpu", tomllib.loads(window.text))
        self.assertIn("Total Estimated VRAM: 0.00 GB", window.resource_totals.cget("text"))
        work = ROOT / "tmp"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="generator-test-", dir=work) as scratch:
            path = Path(scratch) / "generated.toml"
            with patch.object(launcher, "GENERATED_FILE", path):
                window.save()
            self.assertEqual(tomllib.loads(path.read_text()), tomllib.loads(window.text))
        callback.assert_called_once_with()

    def test_advanced_generated_config_restores_before_gpu_detection(self):
        root = launcher.tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        work = ROOT / "tmp"
        work.mkdir(exist_ok=True)
        cases = [
            [("local", "whisper", "whisper_large-v3-q5_0", "", ""),
             ("remote", "llama", None, "desktop.example", "9002"),
             ("local", "kokoro", "kokoro_v1.0", "", "")],
            [("remote", "whisper", None, "192.168.1.8", "9101"),
             ("off", None, None, "", ""),
             ("remote", "qwentts", None, "speech.example", "9105")],
            [("off", None, None, "", "")] * 3,
        ]
        with tempfile.TemporaryDirectory(prefix="restore-generator-", dir=work) as scratch:
            path = Path(scratch) / "generated.toml"
            for selections in cases:
                with self.subTest(selections=selections):
                    text = launcher.assemble_advanced(selections, ["English", "Japanese"], gpu=2)
                    path.write_text(text, encoding="utf-8")
                    with patch.object(launcher, "GENERATED_FILE", path), patch.object(launcher.threading, "Thread"):
                        window = launcher.GenerateConfigWindow(root, Mock())
                        window.window.withdraw()
                        self.assertTrue(window.advanced.get())
                        self.assertEqual(window.recipe_row.winfo_manager(), "")
                        self.assertEqual(window.backend_frame.winfo_manager(), "pack")
                        self.assertEqual([v.get() for v in window.languages], ["English", "Japanese"])
                        self.assertEqual(tomllib.loads(window.text), tomllib.loads(text))
                        # The delayed GPU result must not replace restored selections.
                        window.gpu_found([(2, "Small test GPU", 6), (7, "Large test GPU", 24)], None)
                        self.assertEqual(tomllib.loads(window.text), tomllib.loads(text))
                        window.gpu_found([], "No GPU")
                        self.assertEqual(tomllib.loads(window.text), tomllib.loads(text))
                        window.window.destroy()
            path.write_text(launcher.assemble("nvidia-8gb", launcher.load_recipes()["nvidia-8gb"]), encoding="utf-8")
            with patch.object(launcher, "GENERATED_FILE", path), patch.object(launcher.threading, "Thread"):
                window = launcher.GenerateConfigWindow(root, Mock())
                window.window.withdraw()
                self.assertFalse(window.advanced.get())
                window.window.destroy()


if __name__ == "__main__":
    unittest.main()
