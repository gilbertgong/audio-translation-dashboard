"""Download prebuilt server binaries (whisper.cpp, llama.cpp) from GitHub
releases, or install a server's Python packages, as configured in the
[api_servers] section of the config.

Usage, from the project root:
    python src/install_api_servers.py [--dry-run] [--force] [--only NAME] [config_local.toml]

Per server, in config_local.toml:
    start = false            skip the server entirely
    ensure_version = false   keep whatever build is installed, without contacting
                             GitHub; download only if none is installed, or if
                             release is pinned to a different tag
    ensure_version = true    check GitHub on every run and make sure the
                             configured release is installed (the newest for
                             release = "latest", or exactly a pinned tag)
--force reinstalls the configured release regardless.

Settings come from config/defaults.toml, with config_local.toml (or the given file)
on top. Each server is unpacked (.zip or .tar.*) into <install_dir>/<name>/
(e.g. api_servers/whisper/), with the release tag written to VERSION.

A server run by one of this project's scripts can instead list the Python
packages it needs (packages = ["misaki==0.9.4", ...], pip requirement
specifiers): they're installed with pip install --target <install_dir>/<name>/lib,
for this Python (no virtual environment; the script puts that folder on its
import path). VERSION records the list and the Python version; they're
installed again when either changes (or with --force).

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from common import (CONFIG_LOCAL_FILE, USER_AGENT, api_servers, config_path, download, load_config, run_script,
                    setup_logging, starts, unpack)

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


def lock_installs(install_dir):
    """Hold a lock on <install_dir>/.install.lock until this process exits, so
    two installers (e.g. Launch and a server button) never work on the same
    folders at once: the second waits, then finds what the first installed."""
    install_dir.mkdir(parents=True, exist_ok=True)
    lock = open(install_dir / ".install.lock", "a+")
    waiting = False
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock  # kept open (and locked) for the rest of the run
        except OSError:
            if not waiting:
                print("== waiting for another install to finish", flush=True)
                waiting = True
            time.sleep(1)


def replace(name, dest, staging):
    """Swap the staged install into place without ever half-deleting a working
    one: the old folder is first renamed aside (which fails cleanly, before
    anything is deleted, if a file in it is open), then deleted as far as it
    can be (a running server's loaded libraries stay until it stops; the next
    install clears what's left)."""
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        try:
            dest.rename(old)
        except OSError as err:
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemExit(f"error: could not replace {dest} (is the {name} server still running?): {err}")
    staging.rename(dest)
    shutil.rmtree(old, ignore_errors=True)


def install(name, dest, tag, urls):
    # Download and unpack into a staging directory, then swap it into place,
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
            unpack(archive, staging)
        finally:
            archive.unlink(missing_ok=True)
            archive.with_name(archive.name + ".part").unlink(missing_ok=True)

    # Some archives wrap everything in one top-level folder (e.g. Release/); flatten it.
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = staging.with_name(staging.name + ".inner")
        entries[0].rename(inner)
        staging.rmdir()
        inner.rename(staging)

    (staging / "VERSION").write_text(tag + "\n")
    replace(name, dest, staging)
    print(f"   installed to {dest}")
    log.info("installed %s %s to %s", name, tag, dest)


def packages_tag(packages):
    """What VERSION records for a package list: a short hash of the list, and
    this Python's version (the compiled packages are built for one)."""
    digest = hashlib.sha256("\n".join(packages).encode()).hexdigest()[:12]
    return f"packages-{digest} python{sys.version_info.major}.{sys.version_info.minor}"


def install_packages(name, dest, tag, packages):
    """pip install --target dest/lib, staged like install() so a failed
    install never leaves a half-installed server behind."""
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    command = [sys.executable, "-m", "pip", "install", "--target", str(staging / "lib"),
               "--disable-pip-version-check", "--progress-bar", "off", *packages]
    print(f"   pip install --target {dest / 'lib'} ({len(packages)} packages and their dependencies)")
    log.info("installing %s packages: %s", name, " ".join(packages))
    if subprocess.run(command).returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(f"error: installing {name}'s packages failed (see pip's output above)")
    (staging / "VERSION").write_text(tag + "\n" + "\n".join(packages) + "\n")
    replace(name, dest, staging)
    print(f"   installed to {dest}")
    log.info("installed %s (%s) to %s", name, tag, dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=CONFIG_LOCAL_FILE, type=Path,
                        help="settings on top of the defaults and config_generated.toml "
                             "(default: the project's config_local.toml)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded, change nothing")
    parser.add_argument("--force", action="store_true", help="reinstall the configured release")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="just this server (repeatable), e.g. --only kokoro")
    args = parser.parse_args()

    full_cfg = load_config(args.config)
    setup_logging(full_cfg, NAME)
    cfg = full_cfg["api_servers"]
    install_dir = config_path(full_cfg, cfg.get("install_dir", "api_servers"))
    if not args.dry_run:
        lock = lock_installs(install_dir)  # noqa: F841 (held until exit)

    for name, server in api_servers(full_cfg).items():
        if args.only and name not in args.only:
            continue
        if not starts(full_cfg, name):
            print(f"== {name}: skipped (start = false)")
            log.info("%s skipped (start = false)", name)
            continue
        dest = install_dir / name
        release = server.get("release", "latest")
        version_file = dest / "VERSION"
        installed = version_file.read_text().split("\n")[0].strip() if version_file.is_file() else None

        # Python packages, for one of our scripts (a GitHub release takes
        # precedence: e.g. an older generated config's sherpa-onnx for Kokoro).
        if "packages" in server and "repo" not in server:
            tag = packages_tag(server["packages"])
            if installed == tag and not args.force:
                print(f"== {name}: packages installed ({tag})")
                log.info("%s packages installed (%s)", name, tag)
            elif args.dry_run:
                print(f"== {name}: would install {len(server['packages'])} packages to {dest / 'lib'}")
            else:
                print(f"== {name}: installing packages ({tag})")
                install_packages(name, dest, tag, server["packages"])
            continue

        # A pinned release that differs from the installed one (e.g. after switching
        # this server to another project) is installed even with ensure_version = false.
        pinned_elsewhere = release != "latest" and installed != release
        if installed and not server.get("ensure_version", False) and not args.force and not pinned_elsewhere:
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
