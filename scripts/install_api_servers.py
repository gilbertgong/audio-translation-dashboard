"""Download prebuilt whisper.cpp and llama.cpp binaries from GitHub releases,
as configured in the [api_servers] section of config.toml.

Usage, from the project root:
    python scripts/install_api_servers.py [--dry-run] [--force] [config.toml]

Per server, in config.toml:
    start = false            skip the server entirely
    ensure_version = false   keep whatever build is installed, without contacting
                             GitHub; download only if none is installed
    ensure_version = true    check GitHub on every run and make sure the
                             configured release is installed (the newest for
                             release = "latest", or exactly a pinned tag)
--force reinstalls the configured release regardless.

Settings come from config/defaults.toml, with config.toml (or the given file)
on top. Each server is unzipped into <install_dir>/<name>/ (e.g.
api_servers/whisper/), with the release tag written to VERSION.

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

from common import (CONFIG_FILE, USER_AGENT, api_servers, config_path, download, load_config, run_script,
                    setup_logging, starts)

NAME = "install_api_servers"
log = logging.getLogger(NAME)


def github(path):
    url = "https://api.github.com/repos/" + path
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except OSError as err:  # includes HTTP errors and no network
        raise SystemExit(f"error: could not get {url}: {err}") from None


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
    parser.add_argument("config", nargs="?", default=CONFIG_FILE, type=Path,
                        help="settings over config/defaults.toml (default: the project's config.toml)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded, change nothing")
    parser.add_argument("--force", action="store_true", help="reinstall the configured release")
    args = parser.parse_args()

    full_cfg = load_config(args.config)
    setup_logging(full_cfg, NAME)
    cfg = full_cfg["api_servers"]
    install_dir = config_path(full_cfg, cfg.get("install_dir", "api_servers"))

    for name, server in api_servers(full_cfg).items():
        if not starts(full_cfg, name):
            print(f"== {name}: skipped (start = false)")
            log.info("%s skipped (start = false)", name)
            continue
        dest = install_dir / name
        release = server.get("release", "latest")
        version_file = dest / "VERSION"
        installed = version_file.read_text().strip() if version_file.is_file() else None

        if installed and not server.get("ensure_version", False) and not args.force:
            print(f"== {name}: {installed} (installed; ensure_version = false)")
            log.info("%s %s installed; not checking for a newer release", name, installed)
            continue

        patterns = [p.format(binary_type=cfg["binary_type"]) for p in server["assets"]]
        tag, urls = find_release(server["repo"], release, patterns)

        print(f"== {name}: {tag}")
        for url in urls:
            print(f"   {url.rsplit('/', 1)[-1]}")

        if not args.force and installed == tag:
            print(f"   already installed in {dest}")
            log.info("%s %s already installed in %s", name, tag, dest)
        elif args.dry_run:
            print(f"   would install to {dest}")
        else:
            log.info("installing %s %s from %s", name, tag, ", ".join(urls))
            install(name, dest, tag, urls)


if __name__ == "__main__":
    run_script(main, NAME)
