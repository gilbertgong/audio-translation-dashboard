"""Installer contention and lock-error reporting without installing anything."""

import errno
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import install_api_servers as installer


class InstallLockTests(unittest.TestCase):
    def test_contention_waits_then_acquires_on_both_platforms(self):
        for platform, error in (("nt", errno.EACCES), ("posix", errno.EAGAIN),
                                ("posix", errno.EACCES)):
            with self.subTest(platform=platform, error=error), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                acquire = Mock(side_effect=[OSError(error, "busy"), None])
                backend = ("msvcrt", SimpleNamespace(locking=acquire, LK_NBLCK=1)) if platform == "nt" else (
                    "fcntl", SimpleNamespace(flock=acquire, LOCK_EX=2, LOCK_NB=4))
                with patch.object(installer, "os", SimpleNamespace(name=platform)), \
                        patch.dict(sys.modules, {backend[0]: backend[1]}), \
                        patch.object(installer.time, "sleep") as sleep, patch("builtins.print") as output:
                    lock = installer.lock_installs(folder)
                    self.addCleanup(lock.close)
                    self.assertFalse(lock.closed)
                    self.assertEqual(acquire.call_count, 2)
                    sleep.assert_called_once_with(1)
                    output.assert_called_once_with("== waiting for another install to finish", flush=True)
                    lock.close()

    def test_other_errors_report_path_and_error_without_waiting(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                handle = Mock()
                acquire = Mock(side_effect=OSError(errno.EBADF, "bad descriptor"))
                backend = ("msvcrt", SimpleNamespace(locking=acquire, LK_NBLCK=1)) if platform == "nt" else (
                    "fcntl", SimpleNamespace(flock=acquire, LOCK_EX=2, LOCK_NB=4))
                with patch.object(installer, "os", SimpleNamespace(name=platform)), \
                        patch.dict(sys.modules, {backend[0]: backend[1]}), \
                        patch("builtins.open", return_value=handle), \
                        patch.object(installer.time, "sleep") as sleep, patch("builtins.print") as output:
                    with self.assertRaises(SystemExit) as caught:
                        installer.lock_installs(folder)
                    self.assertIn(str(folder / ".install.lock"), str(caught.exception))
                    self.assertIn("bad descriptor", str(caught.exception))
                    handle.close.assert_called_once()
                    sleep.assert_not_called()
                    output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
