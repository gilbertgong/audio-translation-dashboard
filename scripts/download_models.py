"""Download the whisper.cpp and llama.cpp models listed in the [models] section
of config.toml.

Usage, from the project root:
    python scripts/download_models.py [--dry-run] [--force] [config.toml]

The config defaults to ./config.toml (the current directory). Each model is
saved to <models.dir>/<name>/ (e.g. models/whisper/ggml-large-v3.bin).
Interrupted downloads resume where they left off when run again.

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import sys
from pathlib import Path

from common import download, load_config, model_path, model_url


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=Path("config.toml"), type=Path,
                        help="config file (default: ./config.toml)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded, change nothing")
    parser.add_argument("--force", action="store_true", help="download again even if the file exists")
    args = parser.parse_args()

    cfg = load_config(args.config)
    names = [k for k, v in cfg["models"].items() if k != "dir" and isinstance(v, str)]

    for name in names:
        url = model_url(name, cfg["models"][name])
        dest = model_path(cfg, name)
        print(f"== {name}: {dest.name}")
        print(f"   {url}")

        if dest.is_file() and not args.force:
            print(f"   already downloaded ({dest.stat().st_size / 2**30:.2f} GB) in {dest.parent}")
        elif args.dry_run:
            print(f"   would download to {dest.parent}")
        else:
            if args.force:
                dest.unlink(missing_ok=True)
            download(url, dest, resume=not args.force)
            print(f"   saved to {dest}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted; run again to resume")
        sys.exit(130)
