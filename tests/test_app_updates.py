"""Offline updater checks. Run: python -m unittest discover -s tests -v"""

import io
import json
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import app_updates as updates


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / "tmp"
        work.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="updater-test-", dir=work)).resolve()
        assert self.root.parent == work.resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.local = {"appVersion": "0.1.0", "launcherVersion": "0.1.0",
                      "masterVersionFile": "https://example.test/app.version",
                      "directDownloadURL": "https://example.test/old.zip"}
        self.remote = {**self.local, "appVersion": "0.2.0", "launcherVersion": "0.3.0",
                       "directDownloadURL": "https://example.test/new.zip"}
        self.write("app.version", json.dumps(self.local))

    def write(self, relative, text):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def archive(self, extra=None, version=None):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zipped:
            files = {"app.version": json.dumps(version or self.remote),
                     "src/app.py": "new app", "src/new.py": "new module",
                     "config/defaults.toml": "new defaults",
                     "scripts/launch.sh": "new launch script",
                     "launcher.pyw": "must not replace launcher",
                     "config_local.toml": "must not replace local config",
                     "config_generated.toml": "must not replace generated config",
                     "voices/custom.bin": "must not replace voice",
                     "models/model.bin": "must not replace model",
                     "api_servers/engine.exe": "must not replace engine",
                     "AI/LOCAL.md": "must not replace local notes"}
            files.update(extra or {})
            for name, text in files.items():
                zipped.writestr("release/" + name, text)
        data.seek(0)
        return data

    def prepare(self, archive=None):
        with patch.object(updates, "request", return_value=archive or self.archive()):
            prepared = updates.prepare_update(self.root, self.remote)
        self.addCleanup(prepared.close)
        return prepared

    def test_numeric_versions(self):
        self.assertTrue(updates.newer("0.10.0", "0.9.0"))
        self.assertFalse(updates.newer("1.0", "1.0.0"))
        self.assertFalse(updates.newer("0.1.0", "0.2.0"))

    def test_check_reads_local_master_and_remote_download(self):
        with patch.object(updates, "request", return_value=io.BytesIO(json.dumps(self.remote).encode())) as request:
            local, remote = updates.check_update(self.root)
        request.assert_called_once_with(self.local["masterVersionFile"])
        self.assertEqual(remote["directDownloadURL"], self.remote["directDownloadURL"])
        self.assertTrue(updates.newer(remote["launcherVersion"], local["launcherVersion"]))

    def test_app_update_preserves_launcher_and_user_data(self):
        preserved = ["launcher.pyw", "config_local.toml", "config_generated.toml",
                     "voices/custom.bin", "models/model.bin", "api_servers/engine.exe", "AI/LOCAL.md"]
        for relative in preserved:
            self.write(relative, "keep " + relative)
        self.write("src/app.py", "old app")
        self.write("config/custom.toml", "custom recipe")
        prepared = self.prepare()
        # Downloading/staging changes no installed app files.
        self.assertEqual((self.root / "src/app.py").read_text(), "old app")
        updates.install_update(self.root, prepared)
        self.assertEqual((self.root / "src/app.py").read_text(), "new app")
        self.assertEqual((self.root / "src/new.py").read_text(), "new module")
        self.assertEqual((self.root / "scripts/launch.sh").read_text(), "new launch script")
        self.assertEqual((self.root / "config/custom.toml").read_text(), "custom recipe")
        for relative in preserved:
            self.assertEqual((self.root / relative).read_text(), "keep " + relative)
        installed = updates.read_version(self.root / "app.version")
        self.assertEqual(installed["appVersion"], "0.2.0")
        self.assertEqual(installed["launcherVersion"], "0.1.0")
        self.assertEqual(installed["directDownloadURL"], self.remote["directDownloadURL"])

    def test_failure_rolls_back_replaced_and_new_files(self):
        self.write("src/app.py", "old app")
        self.write("config/defaults.toml", "old defaults")
        prepared = self.prepare()
        replace = Path.replace

        def locked(path, target):
            if path.name == "defaults.toml":
                raise PermissionError("simulated locked file")
            return replace(path, target)

        with patch.object(Path, "replace", locked), self.assertRaisesRegex(RuntimeError, "previous files restored"):
            updates.install_update(self.root, prepared)
        self.assertEqual((self.root / "src/app.py").read_text(), "old app")
        self.assertFalse((self.root / "src/new.py").exists())
        self.assertEqual((self.root / "config/defaults.toml").read_text(), "old defaults")
        self.assertEqual(updates.read_version(self.root / "app.version"), self.local)

    def test_rejects_zip_traversal_before_install(self):
        with self.assertRaisesRegex(ValueError, "unsafe ZIP entry"):
            self.prepare(self.archive({"../escape.py": "bad"}))
        self.assertFalse((self.root / "escape.py").exists())
        self.assertEqual(updates.read_version(self.root / "app.version"), self.local)

    def test_advertised_app_version_matches_archive(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.prepare(self.archive(version=self.local))

    def test_download_failure_leaves_installation_alone(self):
        with patch.object(updates, "request", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                updates.prepare_update(self.root, self.remote)
        self.assertEqual(updates.read_version(self.root / "app.version"), self.local)
        self.assertEqual(list((self.root / "tmp").iterdir()), [])


class LauncherUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = types.ModuleType("launcher_update_test")
        cls.module.__file__ = str(ROOT / "launcher.pyw")
        exec(compile((ROOT / "launcher.pyw").read_text(encoding="utf-8"),
                     cls.module.__file__, "exec"), cls.module.__dict__)

    def launcher(self):
        window = self.module.Launcher.__new__(self.module.Launcher)
        window.root = Mock()
        window.status = Mock()
        window.update_buttons = Mock()
        window.show_versions = Mock()
        window.write = Mock()
        return window

    def test_new_launcher_is_informational_not_an_app_update_gate(self):
        window = self.launcher()
        local = {"appVersion": "0.1.0", "launcherVersion": "0.1.0"}
        remote = {"appVersion": "0.2.0", "launcherVersion": "0.3.0", "directDownloadURL": "https://example.test/full.zip"}
        with patch.object(self.module.messagebox, "askyesno", return_value=True), \
                patch.object(self.module.threading, "Thread") as thread:
            window.offer_update((local, remote, True, True))
        self.assertEqual(window.updating, "download")
        window.show_versions.assert_called_once_with("0.3.0")
        thread.return_value.start.assert_called_once()

    def test_launcher_only_update_does_not_download_or_install(self):
        window = self.launcher()
        local = {"appVersion": "0.1.0", "launcherVersion": "0.1.0"}
        remote = {"appVersion": "0.1.0", "launcherVersion": "0.2.0", "directDownloadURL": "https://example.test/full.zip"}
        with patch.object(self.module.messagebox, "showinfo") as info, \
                patch.object(self.module.threading, "Thread") as thread:
            window.offer_update((local, remote, False, True))
        thread.assert_not_called()
        info.assert_called_once()
        self.assertIsNone(window.updating)
        window.show_versions.assert_called_once_with("0.2.0")

    def test_install_waits_until_owned_processes_have_exited(self):
        window = self.launcher()
        window.mode = "launch"
        process = Mock(pid=123)
        process.poll.return_value = None
        window.solo = {"kokoro": process}
        window.update_killed = set()
        with patch.object(self.module.threading, "Thread") as thread:
            window.wait_for_update_stop()
            thread.assert_called_once_with(target=self.module.stop_tree, args=(process,), daemon=True)
            window.root.after.assert_called_once_with(100, window.wait_for_update_stop)
            window.mode = None
            window.solo = {}
            window.wait_for_update_stop()
        self.assertEqual(window.updating, "install")

    def test_finish_resumes_launch_without_updating_launcher_version(self):
        window = self.launcher()
        window.update_package = Mock()
        window.update_resume = "launch"
        window.update_resume_solo = ["kokoro"]
        window.reload_config = Mock()
        window.start = Mock()
        window.start_solo = Mock()
        remote = {"appVersion": "0.2.0", "launcherVersion": "0.3.0"}
        with patch.object(updates, "read_version", return_value={"appVersion": "0.2.0", "launcherVersion": "0.1.0"}):
            window.finish_update(remote)
        window.start.assert_called_once_with("launch")
        window.start_solo.assert_not_called()
        self.assertIsNone(window.updating)
        window.show_versions.assert_called_once_with("0.3.0")


if __name__ == "__main__":
    unittest.main()
