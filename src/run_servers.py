"""Start the API servers (whisper-server, the TTS server, llama-server) and keep
them running while a command runs, e.g. the dashboard.

Usage, from the project root:
    python src/run_servers.py [--config config_local.toml] [COMMAND ...]

Starts COMMAND first (so the dashboard is up, showing the servers' status as
they start), then each server with start = true in [api_servers], in config
order, waiting until it answers on /health. Voices listed in
[api_servers.<name>.voices] are registered as soon as their server is ready,
or, for a server with voice_arg (e.g. "--voice"), passed on its command line
as voice_arg NAME=FILE (Kokoro reads its voices only when it loads).
The servers are stopped when COMMAND exits. Lines on standard input (typed in
the terminal, or sent by the launcher's developer controls):
    restart        restart COMMAND alone, keeping the servers running: e.g. to
                   pick up changes to the dashboard (Restart app)
    stop NAME      stop one server, e.g. "stop kokoro"
    start NAME     start it again (one with start = true; voices registered)
Each server's state is reported on a line of its own, for the launcher:
"== server NAME: starting" (then ready, stopping, stopped, failed, or exited
when it ends by itself). Without a COMMAND, keeps the servers running until
Ctrl+C.

--only NAME starts just that server (repeatable): the launcher's server
buttons run one on its own this way, before or alongside a launch. A server
that's already running at its address (e.g. started that way) isn't started
again: it's used as it is, and left running when this stops.

    python src/run_servers.py python src/app.py

GPUs: [gpu] devices, or a server's own gpu setting, becomes the server's
CUDA_VISIBLE_DEVICES (by nvidia-smi index: CUDA_DEVICE_ORDER=PCI_BUS_ID);
a server's [api_servers.<name>.env] table adds other environment variables.

Server output goes to logs/<name>.log; launches and errors are also logged to
logs/app.log. The servers must already be installed, with their models
(install_api_servers.py, download_models.py).

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import base64
import json
import os
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
    if "voice_arg" in server:  # voices given at start (see register_voices for the others)
        args += [arg for voice, path in voice_files(cfg, server).items()
                 for arg in (server["voice_arg"], f"{voice}={path}")]
    return [*program, *model_args, "--host", server["host"], "--port", str(server["port"]), *args]


def server_env(cfg, server):
    """The server's environment: ours, with CUDA_VISIBLE_DEVICES from its gpu
    setting or [gpu] devices (by nvidia-smi index), and its env table; plus a
    short description of the GPU choice, for the log."""
    env = dict(os.environ)
    devices = server.get("gpu", cfg.get("gpu", {}).get("devices"))
    gpu = "all GPUs"
    if devices is not None:
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # number the cards as nvidia-smi does
        env["CUDA_VISIBLE_DEVICES"] = str(devices)
        gpu = "no GPU" if str(devices).strip() == "-1" else f"GPU {devices}"
    env.update({key: str(value) for key, value in server.get("env", {}).items()})
    return env, gpu


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
    """Start a server and wait until it's ready; returns its process, or None
    if it was already running (started separately: left alone)."""
    url = server_url(server)
    if health(url) is not None:
        # Already there (e.g. started on its own from the launcher, perhaps still
        # loading): use it as it is.
        print(f"== {name} is already running at {url}: using it (started separately; left running)")
        start = time.monotonic()
        while (status := health(url)) != 200:
            if status is None:
                raise SystemExit(f"error: {name} at {url} went away while loading")
            if time.monotonic() - start > STARTUP_TIMEOUT:
                raise SystemExit(f"error: {name} at {url} not ready after {STARTUP_TIMEOUT}s")
            time.sleep(1)
        log.info("%s already running at %s: used as it is", name, url)
        return None
    argv = server_argv(cfg, name, server)
    env, gpu = server_env(cfg, server)
    server_log = log_dir / f"{name}.log"
    print(f"== Starting {name} at {url}, {gpu} (log: {server_log})")
    with open(server_log, "wb") as out:
        # stdin: not ours (see Command.start).
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env)
    log.info("launched %s (%s) with command\n  %s", name, gpu, subprocess.list2cmdline(argv))

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
    this runs at every start). Speaker-embedding cloning: no transcript. Not
    for a server with voice_arg: its voices were on its command line."""
    if "voice_arg" in server:
        return
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


def report(name, state):
    """A server's state, on a line of its own (the launcher shows it)."""
    print(f"== server {name}: {state}", flush=True)


def end(proc):
    """Terminate a server process, killing it if it doesn't exit in 10 s."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Servers:
    """The running servers, {name: process}; each can be stopped and started
    again on its own (stop NAME / start NAME on standard input)."""

    def __init__(self, cfg, log, log_dir):
        self.cfg, self.log, self.log_dir = cfg, log, log_dir
        self.procs = {}
        self.busy = set()  # being started or stopped on request
        self.lock = threading.Lock()
        self.exited = threading.Event()  # set when one exits by itself
        threading.Thread(target=self.watch, daemon=True).start()

    def start(self, name):
        """Start a server, wait until it's ready and register its voices.
        Raises SystemExit (with the reason) if it fails."""
        server = api_servers(self.cfg)[name]
        report(name, "starting")
        try:
            proc = start_server(self.cfg, name, server, self.log, self.log_dir)
            if proc:  # (None: already running, with its voices; not ours to stop)
                with self.lock:
                    self.procs[name] = proc
                register_voices(self.cfg, name, server, self.log)
        except SystemExit:
            report(name, "failed")
            raise
        report(name, "ready")

    def stop(self, name):
        with self.lock:
            proc = self.procs.pop(name, None)  # popped first, so watch() doesn't report it
        if proc:
            report(name, "stopping")
            self.log.info("stopping %s", name)
            end(proc)
            report(name, "stopped")

    def stop_all(self):
        if self.procs:
            print("== Stopping API servers")
            self.log.info("stopping API servers")
        for name in list(self.procs):
            self.stop(name)

    def request(self, action, name):
        """"stop NAME" or "start NAME" from standard input (each in a thread of
        its own: starting takes a while). A failed start is reported, not fatal."""
        if name not in api_servers(self.cfg) or not starts(self.cfg, name):
            print(f"== {action} {name}: not one of the servers started here", flush=True)
            return
        with self.lock:
            if name in self.busy:
                print(f"== {action} {name}: already being started or stopped", flush=True)
                return
            running = name in self.procs
            if running == (action == "start"):
                print(f"== {name} is already {'running' if running else 'stopped'}", flush=True)
                return
            self.busy.add(name)
        try:
            if action == "stop":
                self.stop(name)
            else:
                self.start(name)
        except SystemExit as err:
            print(err, flush=True)
            self.log.warning("starting %s failed: %s", name, err)
        finally:
            with self.lock:
                self.busy.discard(name)

    def watch(self):
        """Report a server that exits by itself (not stopped from here)."""
        while True:
            time.sleep(1)
            with self.lock:
                exited = [name for name, proc in self.procs.items() if proc.poll() is not None]
                for name in exited:
                    del self.procs[name]
            for name in exited:
                self.exited.set()
                report(name, "exited")
                self.log.warning("%s exited unexpectedly", name)


class Command:
    """The command run alongside the servers (the dashboard): started before
    them, and restarted on a "restart" line on standard input (which also
    takes "stop NAME" and "start NAME" for the servers)."""

    def __init__(self, argv, log, servers):
        self.argv, self.log, self.servers = argv, log, servers
        self.restarting = threading.Event()
        self.start()
        print('== Type "restart" (and Enter) to restart it, keeping the servers running;'
              ' "stop NAME" or "start NAME" for one server')
        threading.Thread(target=self.watch_input, daemon=True).start()

    def start(self):
        print(f"== Starting: {subprocess.list2cmdline(self.argv)}")
        self.log.info("starting %s", subprocess.list2cmdline(self.argv))
        # Its own stdin, not ours: on Windows a child sharing a pipe that a thread
        # here is reading (watch_input) can hang on it while starting.
        self.child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL)

    def watch_input(self):
        for line in sys.stdin:  # until standard input closes
            words = line.split()
            if [w.lower() for w in words] == ["restart"] and self.child.poll() is None:
                print("== Restarting the dashboard (the servers keep running)")
                self.log.info("restarting %s", subprocess.list2cmdline(self.argv))
                self.restarting.set()
                self.child.terminate()
            elif len(words) == 2 and words[0].lower() in ("start", "stop"):
                threading.Thread(target=self.servers.request, args=(words[0].lower(), words[1]),
                                 daemon=True).start()

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
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="start just this server (repeatable), e.g. --only whisper")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run while the servers are up (e.g. python src/app.py)")
    args = parser.parse_args()
    if args.only:
        unknown = [name for name in args.only if name not in api_servers(load_config(args.config))]
        if unknown:
            raise SystemExit(f"error: no such server in [api_servers]: {', '.join(unknown)}")

    cfg = load_config(args.config)
    log = setup_logging(cfg, NAME)
    log_dir = config_path(cfg, LOG_FILE).parent

    # Check the voice files up front, before anything is started.
    for name, server in api_servers(cfg).items():
        if starts(cfg, name):
            for voice, path in voice_files(cfg, server).items():
                if not path.is_file():
                    raise SystemExit(f"error: voice {voice!r} for {name}: {path} not found")

    servers = Servers(cfg, log, log_dir)
    exit_code = 0
    command = None
    try:
        # The dashboard first, so it's up (showing the servers' status) while they start.
        if args.command:
            command = Command(args.command, log, servers)
        # Config order: llama-server comes last, so it sizes itself to the GPU
        # memory the others leave over.
        for name in api_servers(cfg):
            if command and command.exited():
                break  # the dashboard stopped (e.g. its port was taken): no point going on
            if args.only and name not in args.only:
                continue
            if starts(cfg, name):
                servers.start(name)
            else:
                print(f"== {name}: not started (start = false)")
        if command:
            exit_code = command.wait()
        else:
            print("== Servers running; Ctrl+C to stop")
            while not servers.exited.wait(1):  # in steps, so Ctrl+C gets through on Windows
                pass
            log.warning("a server exited unexpectedly")
            exit_code = 1
    except KeyboardInterrupt:
        pass
    finally:
        if command:
            command.stop()
        servers.stop_all()
    sys.exit(exit_code)


if __name__ == "__main__":
    run_script(main, NAME)
