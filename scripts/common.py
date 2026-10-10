"""Shared helpers for the scripts in this directory. Standard library only."""

import logging
import shutil
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "audio-translation-dashboard-scripts"

# Whisper model names (e.g. "large-v3") expand to files in this repository.
WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"

# Shared log file and line format; app.py and launch.sh write the same format.
LOG_FILE = "logs/app.log"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg, name):
    """Append log messages to logs/app.log (next to config.toml); return a logger."""
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


def load_config(path):
    """Load config.toml, exiting with a helpful message if it's missing."""
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"error: config file not found: {path.resolve()}"
                         " (run from the project root, or pass the config path)")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_config_dir"] = path.resolve().parent
    return cfg


def config_path(cfg, value):
    """Resolve a path from the config; relative paths are relative to config.toml."""
    p = Path(value)
    return p if p.is_absolute() else cfg["_config_dir"] / p


def model_url(name, value):
    """URL for a [models] entry: a full URL, or a whisper.cpp model name."""
    if "://" in value:
        return value
    if name == "whisper":
        return WHISPER_MODEL_URL.format(name=value)
    raise SystemExit(f"error: models.{name} must be a URL, got {value!r}")


def model_path(cfg, name):
    """Local path of a [models] entry: <models.dir>/<name>/<file name from URL>."""
    models = cfg["models"]
    filename = Path(urllib.parse.urlsplit(model_url(name, models[name])).path).name
    return config_path(cfg, models.get("dir", "models")) / name / filename


def download(url, dest, resume=False):
    """Download url to dest, showing progress.

    Data goes to <dest>.part and is renamed to dest only when complete. With
    resume=True an existing .part file is continued instead of restarted.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if resume and part.exists() else 0

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
