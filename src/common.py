"""Shared helpers for the scripts (install_api_servers.py, download_models.py,
run_servers.py). Standard library only."""

import sys

if sys.version_info < (3, 11):  # before configuration.py imports tomllib, which needs 3.11
    raise SystemExit(f"error: Python 3.11+ is required (this is {sys.version.split()[0]})")

import logging
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

USER_AGENT = "audio-translation-dashboard-scripts"

# The settings loader, shared with app.py and the launcher (re-exported here).
from configuration import CONFIG_LOCAL_FILE, ROOT, load as load_config  # noqa: E402

# Whisper model names (e.g. "large-v3") expand to files in this repository.
WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"

# Shared log file and line format; app.py writes the same format.
LOG_FILE = "logs/app.log"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg, name):
    """Append log messages to logs/app.log (next to config_local.toml); return a logger."""
    path = config_path(cfg, LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    return logging.getLogger(name)


def run_script(main, name, interrupted="interrupted"):
    """Run a script's main(), recording errors and interruptions in the log.

    Errors raised before the config is loaded can't be logged (the log lives
    next to the config), so they are only printed.
    """
    log = logging.getLogger(name)
    logging_ready = lambda: bool(logging.getLogger().handlers)
    try:
        main()
    except KeyboardInterrupt:
        if logging_ready():
            log.warning(interrupted)
        print(f"\n{interrupted}")
        sys.exit(130)
    except SystemExit as err:
        if isinstance(err.code, str) and logging_ready():
            log.error(err.code.removeprefix("error: "))
        raise
    except Exception:
        if logging_ready():
            log.exception("failed")
        raise


def config_path(cfg, value):
    """Resolve a path from the config; relative paths are relative to the project folder."""
    p = Path(value)
    return p if p.is_absolute() else cfg["_config_dir"] / p


def api_servers(cfg):
    """The [api_servers.<name>] sections, as {name: settings}, in config order."""
    return {k: v for k, v in cfg["api_servers"].items() if isinstance(v, dict)}


def starts(cfg, name):
    """Whether we install and start server `name` (its start setting; default true)."""
    return cfg["api_servers"].get(name, {}).get("start", True)


def model_url(name, value):
    """URL for a model in [models.<name>]: a full URL, or a whisper.cpp model name."""
    if "://" in value:
        return value
    if name == "whisper":
        return WHISPER_MODEL_URL.format(name=value)
    raise SystemExit(f"error: models.{name} must be a URL, got {value!r}")


def model_names(cfg):
    """The names of the [models.<name>] tables (e.g. whisper, llama, kokoro)."""
    return [k for k, v in cfg["models"].items() if isinstance(v, dict)]


def model_dir(cfg, name):
    """Where [models.<name>]'s files go: <models.dir>/<name>/."""
    return config_path(cfg, cfg["models"].get("dir", "models")) / name


def model_files(cfg, name):
    """The files of a [models.<name>] table, as [(flag, url, local path)].

    The table's keys are the server's flags and its values URLs (or, for
    whisper, a whisper.cpp model name), e.g. {model = ..., codec = ...} ->
    --model and --codec. Files are saved to <models.dir>/<name>/<file name
    from URL>.

    A URL to an archive (.zip, .tar.bz2, ...) names the file to use inside it
    after a #, e.g. ".../kokoro-multi-lang-v1_1.tar.bz2#kokoro-multi-lang-v1_1/model.onnx":
    the archive is unpacked into <models.dir>/<name>/ and that file is passed.
    """
    folder = model_dir(cfg, name)
    files = []
    for key, value in cfg["models"][name].items():
        url = model_url(name, value)
        parts = urllib.parse.urlsplit(url)
        files.append((f"--{key}", url, folder / (parts.fragment or Path(parts.path).name)))
    return files


def unpack(archive, dest):
    """Extract a .zip or .tar.* archive into dest."""
    archive = Path(archive)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")  # "data": no absolute paths, links out, etc.


def download(url, dest, resume=False):
    """Download url to dest, showing progress.

    Data goes to <dest>.part and is renamed to dest only when complete. With
    resume=True an existing .part file is continued instead of restarted.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if resume and part.exists() else 0

    url = urllib.parse.urlsplit(url)._replace(fragment="").geturl()  # a "#file in archive" isn't sent
    headers = {"User-Agent": USER_AGENT}
    if done:
        headers["Range"] = f"bytes={done}-"
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as err:
        if err.code == 416 and done:
            # Range not satisfiable: the .part file is unusable; start over.
            part.unlink()
            return download(url, dest)
        raise

    with resp:
        if done and resp.status != 206:
            done = 0  # server ignored the range request; start over
        remaining = int(resp.headers.get("Content-Length") or 0)
        total = done + remaining
        free = shutil.disk_usage(dest.parent).free
        if remaining > free:
            raise SystemExit(f"error: not enough disk space for {dest.name}: "
                             f"need {remaining / 2**30:.1f} GB, {free / 2**30:.1f} GB free")
        if done:
            print(f"   resuming at {done / 2**20:.1f} MB")

        with open(part, "ab" if done else "wb") as out:
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r   {done / 2**20:9.1f} / {total / 2**20:.1f} MB"
                          f" ({100 * done / total:5.1f}%)", end="", flush=True)
    print()

    if total and done != total:
        raise SystemExit(f"error: download of {dest.name} incomplete ({done} of {total} bytes);"
                         " run again to resume")
    part.replace(dest)
