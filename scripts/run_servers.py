"""Start the API servers (whisper-server, tts-server, llama-server) and keep
them running while a command runs, e.g. the dashboard.

Usage, from the project root:
    python scripts/run_servers.py [--config config_local.toml] [COMMAND ...]

Starts each server with start = true in [api_servers], in config order, and
waits until it answers on /health. Voices listed in [api_servers.<name>.voices]
are registered as soon as their server is ready. Then runs COMMAND, and stops
the servers when it exits. Without a COMMAND, keeps the servers running until
Ctrl+C.

    python scripts/run_servers.py python app.py

Server output goes to logs/<name>.log; launches and errors are also logged to
logs/app.log. The servers must already be installed, with their models
(install_api_servers.py, download_models.py).

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from common import (CONFIG_LOCAL_FILE, LOG_FILE, api_servers, config_path, load_config, model_files, run_script,
                    setup_logging, starts)

NAME = "run_servers"
STARTUP_TIMEOUT = 300  # seconds to wait for each server to become ready


def server_url(server):
    return f"http://{server['host']}:{server['port']}"


def server_argv(cfg, name, server):
    """The server's command line: executable, model file(s), host, port and extra args."""
    install_dir = config_path(cfg, cfg["api_servers"].get("install_dir", "api_servers"))
    exe = install_dir / name / server["executable"]
    if not exe.is_file() and exe.with_suffix(".exe").is_file():
        exe = exe.with_suffix(".exe")
    files = model_files(cfg, name)  # [(flag, url, path)], e.g. ("-m", ..., model.gguf)
    needed = [(exe, "install_api_servers.py")] + [(path, "download_models.py") for _, _, path in files]
    for path, hint in needed:
        if not path.is_file():
            raise SystemExit(f"error: {path} not found (run scripts/{hint})")
    model_args = [arg for flag, _, path in files for arg in (flag, str(path))]
    return [str(exe), *model_args, "--host", server["host"], "--port", str(server["port"]),
            *server.get("args", [])]


def health(url):
    """HTTP status of url/health, or None if nothing answers."""
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code  # e.g. 503 while llama-server loads its model
    except OSError:
        return None


def start_server(cfg, name, server, log, log_dir):
    """Start a server and wait until it's ready; returns its process."""
    url = server_url(server)
    if health(url) is not None:
        raise SystemExit(f"error: something is already listening at {url} (are the servers already running?)")
    argv = server_argv(cfg, name, server)
    server_log = log_dir / f"{name}.log"
    print(f"== Starting {name} at {url} (log: {server_log})")
    with open(server_log, "wb") as out:
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT)
    log.info("launched %s with command\n  %s", Path(argv[0]).name, subprocess.list2cmdline(argv))

    start = time.monotonic()
    while health(url) != 200:
        if proc.poll() is not None:
            tail = server_log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
            print("\n".join(tail), file=sys.stderr)
            raise SystemExit(f"error: {name} exited during startup; see {server_log}")
        if time.monotonic() - start > STARTUP_TIMEOUT:
            proc.kill()
            raise SystemExit(f"error: {name} not ready after {STARTUP_TIMEOUT}s; see {server_log}")
        time.sleep(1)
    seconds = round(time.monotonic() - start)
    print(f"   ready after {seconds}s")
    log.info("%s ready at %s after %ss", name, url, seconds)
    return proc


def voice_files(cfg, server):
    """The server's [api_servers.<name>.voices] entries, as {voice name: WAV path}."""
    return {voice: config_path(cfg, path) for voice, path in server.get("voices", {}).items()}


def register_voices(cfg, name, server, log):
    """Register the server's voices (tts-server keeps them in memory only, so
    this runs at every start). Speaker-embedding cloning: no transcript."""
    for voice, path in voice_files(cfg, server).items():
        body = json.dumps({"name": voice, "wav_b64": base64.b64encode(path.read_bytes()).decode()})
        req = urllib.request.Request(server_url(server) + "/v1/audio/voices", data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace").strip()[:300]
            raise SystemExit(f"error: registering voice {voice!r} with {name} failed: HTTP {err.code}: {detail}")
        except OSError as err:
            raise SystemExit(f"error: registering voice {voice!r} with {name} failed: {err}")
        seconds = time.monotonic() - start
        print(f"   voice {voice!r} registered from {path.name} ({seconds:.1f}s)")
        log.info("%s: registered voice %r from %s in %.1fs", name, voice, path, seconds)


def stop_servers(procs, log):
    if not procs:
        return
    print("== Stopping API servers")
    log.info("stopping API servers")
    for proc in procs:
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_command(command, log):
    """Run the command in the foreground; returns its exit code."""
    print(f"== Starting: {subprocess.list2cmdline(command)}")
    log.info("starting %s", subprocess.list2cmdline(command))
    child = subprocess.Popen(command)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue  # Ctrl+C reaches the child too; let it shut down on its own


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=CONFIG_LOCAL_FILE, type=Path,
                        help="settings over config/defaults.toml (default: the project's config_local.toml)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run while the servers are up (e.g. python app.py)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logging(cfg, NAME)
    log_dir = config_path(cfg, LOG_FILE).parent

    # Check the voice files up front, before anything is started.
    for name, server in api_servers(cfg).items():
        if starts(cfg, name):
            for voice, path in voice_files(cfg, server).items():
                if not path.is_file():
                    raise SystemExit(f"error: voice {voice!r} for {name}: {path} not found")

    procs = []
    exit_code = 0
    try:
        # Config order: llama-server comes last, so it sizes itself to the GPU
        # memory the others leave over.
        for name, server in api_servers(cfg).items():
            if starts(cfg, name):
                procs.append(start_server(cfg, name, server, log, log_dir))
                register_voices(cfg, name, server, log)
            else:
                print(f"== {name}: not started (start = false)")
        if args.command:
            exit_code = run_command(args.command, log)
        else:
            print("== Servers running; Ctrl+C to stop")
            while all(proc.poll() is None for proc in procs):
                time.sleep(1)
            log.warning("a server exited unexpectedly")
            exit_code = 1
    except KeyboardInterrupt:
        pass
    finally:
        stop_servers(procs, log)
    sys.exit(exit_code)


if __name__ == "__main__":
    run_script(main, NAME)
