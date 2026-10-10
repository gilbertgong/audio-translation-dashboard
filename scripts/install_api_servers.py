"""Download prebuilt whisper.cpp and llama.cpp binaries from GitHub releases,
as configured in the [api_servers] section of config.toml.

Usage, from the project root:
    python scripts/install_api_servers.py [--dry-run] [--force] [config.toml]

The config defaults to ./config.toml (the current directory). Each server is
unzipped into <install_dir>/<name>/ (e.g. api_servers/whisper/), with the
release tag written to VERSION.

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import fnmatch
import json
import logging
import shutil
import urllib.request
import zipfile
from pathlib import Path

from common import USER_AGENT, config_path, download, load_config, run_script, setup_logging

NAME = "install_api_servers"
log = logging.getLogger(NAME)


def github(path):
    req = urllib.request.Request(
        "https://api.github.com/repos/" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_release(repo, release, patterns):
    """Return (tag, [download URLs]) for the release whose files match every pattern."""
    if release == "latest":
        # Binaries live on b#### pre-release builds, so GitHub's own
        # "latest release" can't be used; take the newest one that matches.
        candidates = github(f"{repo}/releases?per_page=30")
    else:
        candidates = [github(f"{repo}/releases/tags/{release}")]

    for rel in candidates:
        if rel.get("draft"):
            continue
        assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
        urls = []
        for pattern in patterns:
            matches = sorted(n for n in assets if fnmatch.fnmatchcase(n, pattern))
            if not matches:
                break
            urls.append(assets[matches[-1]])
        else:
            return rel["tag_name"], urls

    raise SystemExit(f"error: no release of {repo} ({release}) has files matching {patterns}")


def install(name, dest, tag, urls):
    # Download and unzip into a staging directory, then swap it into place,
    # so a failed download never leaves a half-installed server behind.
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    for url in urls:
        filename = url.rsplit("/", 1)[-1]
        archive = staging.with_name(filename)
        print(f"   downloading {filename}")
        try:
            download(url, archive)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(staging)
        finally:
            archive.unlink(missing_ok=True)
            archive.with_name(archive.name + ".part").unlink(missing_ok=True)

    # Some zips wrap everything in one top-level folder (e.g. Release/); flatten it.
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = staging.with_name(staging.name + ".inner")
        entries[0].rename(inner)
        staging.rmdir()
        inner.rename(staging)

    (staging / "VERSION").write_text(tag + "\n")
    try:
        if dest.exists():
            shutil.rmtree(dest)
    except OSError as err:
        raise SystemExit(f"error: could not replace {dest} (is the {name} server still running?): {err}")
    staging.rename(dest)
    print(f"   installed to {dest}")
    log.info("installed %s %s to %s", name, tag, dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=Path("config.toml"), type=Path,
                        help="config file (default: ./config.toml)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded, change nothing")
    parser.add_argument("--force", action="store_true", help="reinstall even if already up to date")
    args = parser.parse_args()

    full_cfg = load_config(args.config)
    setup_logging(full_cfg, NAME)
    cfg = full_cfg["api_servers"]
    install_dir = config_path(full_cfg, cfg.get("install_dir", "api_servers"))

    servers = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    for name, server in servers.items():
        patterns = [p.format(binary_type=cfg["binary_type"]) for p in server["assets"]]
        tag, urls = find_release(server["repo"], server.get("release", "latest"), patterns)
        dest = install_dir / name

        print(f"== {name}: {tag}")
        for url in urls:
            print(f"   {url.rsplit('/', 1)[-1]}")

        version_file = dest / "VERSION"
        if not args.force and version_file.is_file() and version_file.read_text().strip() == tag:
            print(f"   already installed in {dest}")
            log.info("%s %s already installed in %s", name, tag, dest)
        elif args.dry_run:
            print(f"   would install to {dest}")
        else:
            log.info("installing %s %s from %s", name, tag, ", ".join(urls))
            install(name, dest, tag, urls)


if __name__ == "__main__":
    run_script(main, NAME)
