"""Release checks use a local bare remote and fake gh; nothing is published."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
if not BASH and Path("C:/Program Files/Git/bin/bash.exe").is_file():
    BASH = "C:/Program Files/Git/bin/bash.exe"


@unittest.skipUnless(BASH and shutil.which("git"), "Bash and Git required")
class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / "tmp"
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="release-test-", dir=work)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.repo = self.folder / "checkout"
        self.repo.mkdir()
        self.remote = self.folder / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        (self.repo / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/release.sh", self.repo / "scripts/release.sh")
        self.launcher_files = ["launcher.pyw", "src/configuration.py", "src/common.py", "src/app_updates.py"]
        for name in [*self.launcher_files, "src/app.py"]:
            path = self.repo / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("original\n")
        self.manifest = self.repo / "app.version"
        self.manifest.write_text(json.dumps({
            "appVersion": "0.4.1", "launcherVersion": "0.3.0",
            "directDownloadURL": "https://github.com/example/project/archive/refs/tags/v0.4.1.zip",
            "masterVersionFile": "https://example.test/master.version"}))
        self.commit("Initial")
        self.git("tag", "v0.3.0")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", "main", "--tags")
        self.gh_log = self.folder / "gh.log"

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.STDOUT, text=True).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def release(self, version="0.4.2"):
        env = dict(os.environ, PYTHON=sys.executable.replace("\\", "/"), GH_LOG=self.gh_log.as_posix())
        command = '''gh() {
  case "$1 $2" in
    "auth status") return 0 ;;
    "repo view") echo example/project ;;
    "release create") printf '%s\\n' "$*" >> "$GH_LOG" ;;
    *) return 99 ;;
  esac
}
export -f gh
bash "$1" "$2"
'''
        return subprocess.run([BASH, "-c", command, "release-check",
                               (self.repo / "scripts/release.sh").as_posix(), version],
                              cwd=self.folder, env=env, text=True, capture_output=True)

    def test_app_only_release_keeps_launcher_version_and_publishes_matching_refs(self):
        (self.repo / "src/app.py").write_text("changed app\n")
        self.commit("App change")
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["appVersion"], "0.4.2")
        self.assertEqual(manifest["launcherVersion"], "0.3.0")
        self.assertTrue(manifest["directDownloadURL"].endswith("/v0.4.2.zip"))
        self.assertEqual(manifest["masterVersionFile"], "https://example.test/master.version")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "v0.4.2^{commit}"))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/main"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("release create v0.4.2 --repo example/project --verify-tag", self.gh_log.read_text())

    def test_each_launcher_file_triggers_version_bump(self):
        for index, name in enumerate(self.launcher_files, 2):
            with self.subTest(name=name):
                (self.repo / name).write_text("changed launcher code\n")
                self.commit("Change " + name)
                version = f"0.4.{index}"
                result = self.release(version)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(self.manifest.read_text())["launcherVersion"], version)

    def test_dirty_tree_does_not_change_version_or_publish(self):
        original = self.manifest.read_bytes()
        (self.repo / "launcher.pyw").write_text("uncommitted\n")
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit or stash", result.stderr)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertFalse(self.gh_log.exists())

    def test_missing_recorded_tag_does_not_guess_launcher_version(self):
        data = json.loads(self.manifest.read_text())
        data["launcherVersion"] = "0.2.9"
        self.manifest.write_text(json.dumps(data))
        self.commit("Missing launcher tag")
        original = self.manifest.read_bytes()
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recorded launcher tag v0.2.9 is unavailable", result.stderr)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertFalse(self.gh_log.exists())

    def test_non_increasing_version_does_not_publish(self):
        result = self.release("0.4.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be newer", result.stderr)
        self.assertFalse(self.gh_log.exists())


if __name__ == "__main__":
    unittest.main()
