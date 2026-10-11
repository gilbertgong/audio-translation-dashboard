"""configuration.py - reading the dashboard's settings; shared by app.py, the
scripts and the launcher, so all of them see the same thing.

Settings come in layers, each on top of the one before:
    config/defaults.toml     every setting, documented (required)
    config_generated.toml    written by the launcher's Generate config (optional)
    config_local.toml        your own settings (optional)
Tables merge key by key; any other value (lists included) is replaced. Relative
paths in settings are relative to the project folder.

Standard library only (Python 3.11+ for tomllib).
"""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the project folder (this file is in src/)
DEFAULTS_FILE = ROOT / "config" / "defaults.toml"
GENERATED_FILE = ROOT / "config_generated.toml"
CONFIG_LOCAL_FILE = ROOT / "config_local.toml"
WHISPER_LANGUAGES_FILE = ROOT / "config" / "whisper_languages.json"  # see whisper_languages()


def whisper_languages():
    """whisper's languages as [(code, name)], in whisper's order, e.g.
    ("zh", "chinese"); [] if the file is missing or unreadable."""
    try:
        with open(WHISPER_LANGUAGES_FILE, encoding="utf-8") as f:
            return [(entry["code"], entry["name"]) for entry in json.load(f)["languages"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def merge(base, override):
    """Settings from override on top of base: tables merge key by key; any
    other value (including lists) is replaced."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def layers(local=CONFIG_LOCAL_FILE):
    """The settings files in effect, in order. local stands in for
    config_local.toml (e.g. a file given on a command line); config_local.toml
    itself may be missing, but a file given explicitly must exist."""
    local = Path(local)
    if not DEFAULTS_FILE.is_file():
        raise SystemExit(f"error: {DEFAULTS_FILE} not found")
    if not local.is_file() and local.resolve() != CONFIG_LOCAL_FILE:
        raise SystemExit(f"error: config file not found: {local.resolve()}")
    return [path for path in (DEFAULTS_FILE, GENERATED_FILE, local) if path.is_file()]


def load(local=CONFIG_LOCAL_FILE):
    """The merged settings. Also sets "_config_dir" (the project folder, for
    relative paths) and "_files" (the layers used, as paths)."""
    files = layers(local)
    settings = {}
    for path in files:
        with open(path, "rb") as f:
            try:
                settings = merge(settings, tomllib.load(f))
            except tomllib.TOMLDecodeError as err:
                raise SystemExit(f"error: {path.resolve()}: {err}") from None
    settings["_config_dir"] = ROOT
    settings["_files"] = [path.resolve() for path in files]
    return settings
