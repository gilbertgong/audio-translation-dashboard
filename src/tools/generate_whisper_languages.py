"""Generate config/whisper_languages.json: the languages whisper recognizes,
read from the installed whisper.cpp library.

Usage, from the project root:
    python src/tools/generate_whisper_languages.py [--dry-run] [config_local.toml]

Loads the whisper library from the whisper server's install folder
(<install_dir>/whisper/, from [api_servers]; installed by
install_api_servers.py) and asks it for every language, through its C API:
whisper_lang_max_id(), whisper_lang_str(id) (the code, e.g. "zh") and
whisper_lang_str_full(id) (the name, e.g. "chinese"). No model is needed. The
file lists them in whisper's order, one per line, with the whisper.cpp release
it came from. Run it again after a whisper.cpp update that adds languages
(they're fixed by the model's vocabulary, so that's rare: large-v3 added one).

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/, for common and configuration

from common import CONFIG_LOCAL_FILE, config_path, load_config, run_script, setup_logging
from configuration import WHISPER_LANGUAGES_FILE

NAME = "generate_whisper_languages"
LIBRARY_NAMES = {"win32": "whisper.dll", "darwin": "libwhisper.dylib"}  # else libwhisper.so


def whisper_languages(folder):
    """[(code, name)] from the whisper library in folder, in whisper's order."""
    library = folder / LIBRARY_NAMES.get(sys.platform, "libwhisper.so")
    if not library.is_file():
        raise SystemExit(f"error: {library} not found (run src/install_api_servers.py)")
    if os.name == "nt":
        os.add_dll_directory(str(folder))  # its ggml libraries sit next to it
    lib = ctypes.CDLL(str(library))
    lib.whisper_lang_max_id.restype = ctypes.c_int
    for function in (lib.whisper_lang_str, lib.whisper_lang_str_full):
        function.argtypes, function.restype = [ctypes.c_int], ctypes.c_char_p
    return [(lib.whisper_lang_str(i).decode(), lib.whisper_lang_str_full(i).decode())
            for i in range(lib.whisper_lang_max_id() + 1)]


def to_json(languages, source):
    data = {
        "description": ("The languages whisper recognizes, in its own order (roughly by how much training data "
                        "each had). A language is given to whisper as its code or its lowercase name, exactly; "
                        "[languages] should use one of these names."),
        "source": source,
        "languages": [{"code": code, "name": name} for code, name in languages],
    }
    text = json.dumps(data, indent=2, ensure_ascii=False)
    # One language per line keeps the file short and diffable.
    text = text.replace('{\n      "code": ', '{"code": ').replace(',\n      "name": ', ', "name": ')
    return text.replace('\n    }', '}') + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=CONFIG_LOCAL_FILE, type=Path,
                        help="settings on top of the defaults and config_generated.toml "
                             "(default: the project's config_local.toml)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logging(cfg, NAME)
    folder = config_path(cfg, cfg["api_servers"].get("install_dir", "api_servers")) / "whisper"
    version_file = folder / "VERSION"
    version = version_file.read_text().strip() if version_file.is_file() else "unknown version"

    languages = whisper_languages(folder)
    source = (f"whisper.cpp {version}: whisper_lang_str() and whisper_lang_str_full() for ids 0 to "
              f"whisper_lang_max_id(), from {folder.relative_to(cfg['_config_dir']).as_posix()}/"
              f"{LIBRARY_NAMES.get(sys.platform, 'libwhisper.so')}")
    text = to_json(languages, source)

    old = WHISPER_LANGUAGES_FILE.read_text(encoding="utf-8") if WHISPER_LANGUAGES_FILE.is_file() else ""
    print(f"== whisper.cpp {version}: {len(languages)} languages")
    if text == old:
        print(f"   {WHISPER_LANGUAGES_FILE.name} is up to date")
        return
    if old:
        before = {entry["code"]: entry["name"] for entry in json.loads(old)["languages"]}
        added = [f"{code}={name}" for code, name in languages if code not in before]
        removed = [f"{code}={name}" for code, name in before.items() if code not in dict(languages)]
        print(f"   added: {', '.join(added) or 'none'}; removed: {', '.join(removed) or 'none'}")
    if args.dry_run:
        print(f"   would write {WHISPER_LANGUAGES_FILE}")
        return
    WHISPER_LANGUAGES_FILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"   wrote {WHISPER_LANGUAGES_FILE}")
    log.info("wrote %s: %d languages from whisper.cpp %s", WHISPER_LANGUAGES_FILE, len(languages), version)


if __name__ == "__main__":
    run_script(main, NAME)
