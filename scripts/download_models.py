"""Download the whisper.cpp and llama.cpp models listed in the [models] section
of config_local.toml.

Usage, from the project root:
    python scripts/download_models.py [--dry-run] [--force] [config_local.toml]

Settings come from config/defaults.toml, with config_local.toml (or the given file)
on top. Each model is saved to <models.dir>/<name>/ (e.g.
models/whisper/ggml-large-v3.bin). Interrupted downloads resume where they left
off when run again. Models for servers set to start = false in
[api_servers.<name>] are skipped.

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
from pathlib import Path

from common import (CONFIG_LOCAL_FILE, download, load_config, model_files, model_names, run_script,
                    setup_logging, starts)

NAME = "download_models"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=CONFIG_LOCAL_FILE, type=Path,
                        help="settings over config/defaults.toml (default: the project's config_local.toml)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded, change nothing")
    parser.add_argument("--force", action="store_true", help="download again even if the file exists")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logging(cfg, NAME)
    for name in model_names(cfg):
        if not starts(cfg, name):
            print(f"== {name}: skipped (start = false)")
            log.info("%s model skipped (start = false)", name)
            continue
        for _flag, url, dest in model_files(cfg, name):
            print(f"== {name}: {dest.name}")
            print(f"   {url}")

            if dest.is_file() and not args.force:
                print(f"   already downloaded ({dest.stat().st_size / 2**30:.2f} GB) in {dest.parent}")
                log.info("%s model already downloaded: %s", name, dest)
            elif args.dry_run:
                print(f"   would download to {dest.parent}")
            else:
                if args.force:
                    dest.unlink(missing_ok=True)
                log.info("downloading %s model from %s", name, url)
                download(url, dest, resume=not args.force)
                print(f"   saved to {dest}")
                log.info("downloaded %s model to %s", name, dest)


if __name__ == "__main__":
    run_script(main, NAME, interrupted="interrupted; run again to resume")
