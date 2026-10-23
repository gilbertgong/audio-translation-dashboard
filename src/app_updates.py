"""ZIP app updates used by the launcher. Standard library only.

The launcher stays running and is never replaced. Install only application-owned
files; user configuration, voices, engines, models, logs and Git metadata stay put.
"""

import json
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION_FILE = "app.version"
APP_FOLDERS = {"src", "config", "scripts"}
APP_FILES = {"README.md", "DESIGN.md", "INSTALL_MAC.md", "LICENSE"}


def read_version(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def newer(remote, local):
    """Compare dotted numeric release versions, e.g. 0.10.0 > 0.9.0."""
    def parts(version):
        values = [int(part) for part in version.split(".")]
        while values and values[-1] == 0:
            values.pop()
        return tuple(values)
    return parts(remote) > parts(local)


def request(url):
    return urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": "audio-translation-dashboard-updater"}), timeout=60)


def check_update(root):
    local = read_version(Path(root) / VERSION_FILE)
    with request(local["masterVersionFile"]) as response:
        remote = json.load(response)
    return local, remote


def inside(root, relative):
    """Keep archive writes within the chosen staging/install folder, including symlinks."""
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"update path leaves installation folder: {relative}")
    return path


def remove_staging(folder, work):
    folder, work = folder.resolve(), work.resolve()
    if folder.parent != work or not folder.name.startswith("app-update-"):
        raise ValueError(f"not an updater staging folder: {folder}")
    shutil.rmtree(folder)


class PreparedUpdate:
    def __init__(self, folder, work, source, files, remote):
        self.folder, self.work = folder, work
        self.source, self.files, self.remote = source, files, remote
        self.keep_backup = False

    def close(self):
        if not self.keep_backup:
            remove_staging(self.folder, self.work)


def prepare_update(root, remote):
    """Download and stage before stopping any processes. Accept GitHub's ZIP wrapper folder."""
    work = Path(root) / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="app-update-", dir=work))
    try:
        archive = folder / "release.zip"
        with request(remote["directDownloadURL"]) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        source = folder / "new"
        source.mkdir()
        files = []
        with zipfile.ZipFile(archive) as zipped:
            manifests = [info for info in zipped.infolist()
                         if PurePosixPath(info.filename).name == VERSION_FILE
                         and len(PurePosixPath(info.filename).parts) <= 2]
            if len(manifests) != 1:
                raise ValueError("release ZIP must contain one app.version at its root")
            prefix = PurePosixPath(manifests[0].filename).parent
            packaged = json.loads(zipped.read(manifests[0]).decode("utf-8"))
            if packaged["appVersion"] != remote["appVersion"]:
                raise ValueError("downloaded app version does not match the advertised version")
            for info in zipped.infolist():
                name = PurePosixPath(info.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in info.filename or ":" in info.filename:
                    raise ValueError(f"unsafe ZIP entry: {info.filename}")
                if info.is_dir() or not name.is_relative_to(prefix):
                    continue
                relative = name.relative_to(prefix)
                if not relative.parts or (relative.parts[0] not in APP_FOLDERS and str(relative) not in APP_FILES):
                    continue
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"symlink in release ZIP: {relative}")
                destination = inside(source, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as incoming, destination.open("wb") as output:
                    shutil.copyfileobj(incoming, output)
                permissions = (info.external_attr >> 16) & 0o777
                if permissions:
                    destination.chmod(permissions)
                files.append(Path(relative))
        if Path("src/app.py") not in files:
            raise ValueError("release ZIP has no src/app.py")
        return PreparedUpdate(folder, work, source, files, remote)
    except Exception:
        remove_staging(folder, work)
        raise


def install_update(root, prepared):
    """Overlay staged app files, restoring replaced files if installation fails.

    Called only after the launcher's own app/server processes have stopped.
    Never prune unrelated files or replace launcher.pyw. Record the installed
    launcher version, rather than the launcher version advertised by the release.
    """
    root = Path(root).resolve()
    local = read_version(root / VERSION_FILE)
    installed = dict(prepared.remote)
    installed["launcherVersion"] = local["launcherVersion"]
    (prepared.source / VERSION_FILE).write_text(
        json.dumps(installed, indent=2) + "\n", encoding="utf-8", newline="\n")
    backup = prepared.folder / "backup"
    backup.mkdir()
    changed = []
    try:
        for relative in [*prepared.files, Path(VERSION_FILE)]:
            target = inside(root, relative)
            previous = inside(backup, relative) if target.exists() else None
            if previous is not None:
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, previous)
            target.parent.mkdir(parents=True, exist_ok=True)
            inside(prepared.source, relative).replace(target)
            changed.append((target, previous))
    except Exception as error:
        failures = []
        for target, previous in reversed(changed):
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    shutil.copy2(previous, target)
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            # Keep the backup available for recovery rather than deleting it.
            prepared.keep_backup = True
            raise RuntimeError(f"{error}; restoration failed: {'; '.join(failures)}. Backup: {backup}") from error
        raise RuntimeError(f"Update failed; previous files restored: {error}") from error
