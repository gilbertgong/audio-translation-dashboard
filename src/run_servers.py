"""Start the API servers (whisper-server, the TTS server, llama-server) and keep
them running while a command runs, e.g. the dashboard.

Usage, from the project root:
    python src/run_servers.py [--config config_local.toml] [COMMAND ...]

Starts COMMAND first (so the dashboard is up, showing the servers' status as
they start), then each server with start = true in [api_servers], in config
order, waiting until it answers on /health. Voices listed in
[api_servers.<name>.voices] are registered as soon as their server is ready.
The servers are stopped when COMMAND exits. A line "restart" on standard input
(typed in the terminal, or sent by the launcher's Restart app) restarts
COMMAND alone, keeping the servers running: e.g. to pick up changes to the
dashboard. Without a COMMAND, keeps the servers running until Ctrl+C.

    python src/run_servers.py python src/app.py

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
import threading
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
    """The server's command line: executable, model file(s), host, port and extra args.

    A server is either an executable from its install folder, or a Python
    script of this project (script = "..."), run with this Python; "{server_dir}"
    in args is replaced by its install folder (e.g. for the libraries it loads).
    """
    install_dir = config_path(cfg, cfg["api_servers"].get("install_dir", "api_servers"))
    server_dir = install_dir / name
    if "script" in server:
        program = [sys.executable, str(config_path(cfg, server["script"]))]
        needed = [(server_dir / "VERSION", "install_api_servers.py")]
    else:
        exe = server_dir / server["executable"]
        if not exe.is_file() and exe.with_suffix(".exe").is_file():
            exe = exe.with_suffix(".exe")
        program = [str(exe)]
        needed = [(exe, "install_api_servers.py")]
    files = model_files(cfg, name)  # [(flag, url, path)], e.g. ("--model", ..., model.gguf)
    needed += [(path, "download_models.py") for _, _, path in files]
    for path, hint in needed:
        if not path.is_file():
            raise SystemExit(f"error: {path} not found (run src/{hint})")
    model_args = [arg for flag, _, path in files for arg in (flag, str(path))]
    args = [arg.replace("{server_dir}", str(server_dir)) for arg in server.get("args", [])]
    return [*program, *model_args, "--host", server["host"], "--port", str(server["port"]), *args]


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
        # stdin: not ours (see Command.start).
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    log.info("launched %s with command\n  %s", name, subprocess.list2cmdline(argv))

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


class Command:
    """The command run alongside the servers (the dashboard): started before
    them, and restarted on a "restart" line on standard input."""

    def __init__(self, argv, log):
        self.argv, self.log = argv, log
        self.restarting = threading.Event()
        self.start()
        print('== Type "restart" (and Enter) to restart it, keeping the servers running')
        threading.Thread(target=self.watch_input, daemon=True).start()

    def start(self):
        print(f"== Starting: {subprocess.list2cmdline(self.argv)}")
        self.log.info("starting %s", subprocess.list2cmdline(self.argv))
        # Its own stdin, not ours: on Windows a child sharing a pipe that a thread
        # here is reading (watch_input) can hang on it while starting.
        self.child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL)

    def watch_input(self):
        for line in sys.stdin:  # until standard input closes
            if line.strip().lower() == "restart" and self.child.poll() is None:
                print("== Restarting the dashboard (the servers keep running)")
                self.log.info("restarting %s", subprocess.list2cmdline(self.argv))
                self.restarting.set()
                self.child.terminate()

    def exited(self):
        """Whether it has exited by itself (not for a restart)."""
        return self.child.poll() is not None and not self.restarting.is_set()

    def wait(self):
        """Wait until it exits by itself, restarting it whenever asked; returns
        its exit code."""
        while True:
            try:
                code = self.child.wait()
            except KeyboardInterrupt:
                continue  # Ctrl+C reaches the child too; let it shut down on its own
            if not self.restarting.is_set():
                return code
            self.restarting.clear()
            self.start()

    def stop(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=CONFIG_LOCAL_FILE, type=Path,
                        help="settings on top of the defaults and config_generated.toml "
                             "(default: the project's config_local.toml)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run while the servers are up (e.g. python src/app.py)")
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
    command = None
    try:
        # The dashboard first, so it's up (showing the servers' status) while they start.
        if args.command:
            command = Command(args.command, log)
        # Config order: llama-server comes last, so it sizes itself to the GPU
        # memory the others leave over.
        for name, server in api_servers(cfg).items():
            if command and command.exited():
                break  # the dashboard stopped (e.g. its port was taken): no point going on
            if starts(cfg, name):
                procs.append(start_server(cfg, name, server, log, log_dir))
                register_voices(cfg, name, server, log)
            else:
                print(f"== {name}: not started (start = false)")
        if command:
            exit_code = command.wait()
        else:
            print("== Servers running; Ctrl+C to stop")
            while all(proc.poll() is None for proc in procs):
                time.sleep(1)
            log.warning("a server exited unexpectedly")
            exit_code = 1
    except KeyboardInterrupt:
        pass
    finally:
        if command:
            command.stop()
        stop_servers(procs, log)
    sys.exit(exit_code)


if __name__ == "__main__":
    run_script(main, NAME)
