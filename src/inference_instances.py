"""Manage inference instances from an explicit machine TOML file.

Linux/NVIDIA service supervision with Python 3.11+. Local mode stops containers
started by this run; server mode leaves them running. Missing Docker images and
models are provisioned using the configured sources. No proxy or host-agent API.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request

from configuration import ROOT, merge
from common import config_path
from docker_instances import DockerMixin
from run_servers import register_voices, server_argv, server_env, server_url

log = logging.getLogger("inference-supervisor")


def read_toml(path):
    with path.open("rb") as file:
        return tomllib.load(file)


def load_machine(path):
    """Use shared defaults/recipe, then machine settings; no desktop overrides."""
    machine = read_toml(Path(path))
    cfg = read_toml(ROOT / "config/defaults.toml")
    recipe = machine.get("supervisor", {}).get("recipe")
    if recipe:
        recipe = json.loads((ROOT / "config/recipes" / (recipe + ".json")).read_text())
        for fragment in recipe["fragments"]:
            cfg = merge(cfg, read_toml(ROOT / "config/fragments" / (fragment + ".toml")))
        for service in recipe.get("off", []):
            cfg["api_servers"][service]["start"] = False
    cfg = merge(cfg, machine)
    cfg["_config_dir"] = ROOT
    return cfg


def stop_group(proc, grace):
    """Stop the whole owned process group, including subprocesses of wrappers."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            proc.wait()
            return
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


class Instance:
    def __init__(self, cfg, name, settings, stopping, log_dir, mode="local"):
        self.cfg, self.name = cfg, name
        self.service = settings["service"]
        self.server = merge(cfg["api_servers"][self.service], settings)
        self.settings, self.stopping = settings, stopping
        self.mode = mode
        supervisor = cfg.get("supervisor", {})
        self.retry = supervisor.get("restart_delay_s", 5)
        self.grace = supervisor.get("stop_timeout_s", 10)
        self.start_timeout = self.server.get("startup_timeout_s", 300)
        self.health_path = self.server.get("health_path", "/health")
        self.log_path = log_dir / (name + ".log")
        self.ready = threading.Event()
        self.proc = None
        self.process_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name=name, daemon=True)

    def stop_child(self):
        with self.process_lock:
            proc, self.proc = self.proc, None
        if proc is not None:
            stop_group(proc, self.grace)
            log.info("%s: stopped", self.name)

    def command(self, check_files=True):
        if "command" in self.server:
            return self.server["command"]
        paths = merge(self.cfg.get("supervisor", {}).get("model_paths", {}).get(self.service, {}),
                      self.server.get("model_paths", {}))
        files = [("--" + flag, "", config_path(self.cfg, path))
                 for flag, path in paths.items()] if paths else None
        return server_argv(self.cfg, self.service, self.server, files=files, check_files=check_files)

    def wait_ready(self, proc):
        if not self.health_path:
            return proc.poll() is None
        deadline = time.monotonic() + self.start_timeout
        url = server_url(self.server) + self.health_path
        while not self.stopping.is_set():
            if proc.poll() is not None:
                raise RuntimeError(f"exited during startup with status {proc.returncode}")
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return True
            except (OSError, urllib.error.HTTPError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"not ready after {self.start_timeout}s")
            self.stopping.wait(0.2)
        return False

    def run(self):
        while not self.stopping.is_set():
            proc = None
            try:
                dependencies = self.settings.get("after", [])
                if not all(self.instances[name].ready.is_set() for name in dependencies):
                    self.stopping.wait(0.2)
                    continue
                if self.mode == "local" and self.endpoint_ready():
                    log.info("%s: using existing endpoint; left running on exit", self.name)
                    self.ready.set()
                    self.stopping.wait()
                    return
                argv = self.command()
                env, gpu = server_env(self.cfg, self.server)
                with self.log_path.open("ab", buffering=0) as output:
                    log.info("%s: starting (%s); output: %s", self.name, gpu, self.log_path)
                    with self.process_lock:
                        if self.stopping.is_set():
                            break
                        proc = subprocess.Popen(argv, env=env, cwd=config_path(
                            self.cfg, self.server.get("cwd", ".")), stdin=subprocess.DEVNULL,
                            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                        self.proc = proc
                    if not self.wait_ready(proc):
                        break
                    # Instances share the reference-audio cache and its .part files.
                    with self.voice_lock:
                        if self.stopping.is_set():
                            break
                        register_voices(self.cfg, self.name, self.server, log)
                    if self.stopping.is_set():
                        break
                    self.ready.set()
                    log.info("%s: ready (pid %s)", self.name, proc.pid)
                    while not self.stopping.wait(0.2):
                        if proc.poll() is not None:
                            raise RuntimeError(f"exited with status {proc.returncode}")
            except (Exception, SystemExit) as error:
                log.warning("%s: %s; retry in %ss", self.name, error, self.retry)
            finally:
                self.ready.clear()
                self.stop_child()
            self.stopping.wait(self.retry)

    def endpoint_ready(self):
        if not self.health_path:
            return False
        try:
            with urllib.request.urlopen(server_url(self.server) + self.health_path, timeout=1) as response:
                return response.status == 200
        except OSError:
            return False


class DockerInstance(DockerMixin, Instance):
    """Docker lifecycle with the shared instance configuration/readiness helpers."""


class Supervisor:
    native_class = Instance
    docker_class = DockerInstance

    def __init__(self, cfg, mode="local"):
        self.stopping = threading.Event()
        self.cfg = cfg
        folder = config_path(cfg, cfg.get("supervisor", {}).get("log_dir", "logs/inference-supervisor"))
        folder.mkdir(parents=True, exist_ok=True)
        self.instances = {}
        for name, settings in cfg.get("instances", {}).items():
            if not settings.get("start", True):
                continue
            server = merge(cfg["api_servers"][settings["service"]], settings)
            if "docker" in server:
                cls = self.docker_class
            else:
                cls = self.native_class
            self.instances[name] = cls(cfg, name, settings, self.stopping, folder, mode)
        voice_lock = threading.Lock()
        for instance in self.instances.values():
            instance.instances = self.instances
            instance.voice_lock = voice_lock

    def run(self):
        try:
            for instance in self.instances.values():
                instance.thread.start()
            log.info("supervising %s instances", len(self.instances))
            self.stopping.wait()
        finally:
            self.stopping.set()
            deadline = time.monotonic() + self.cfg.get("supervisor", {}).get("stop_timeout_s", 10) + 2
            for instance in self.instances.values():
                if instance.thread.ident is not None:
                    instance.thread.join(timeout=max(0, deadline - time.monotonic()))
            # Voice initialization may still be waiting on HTTP. Stop its child
            # independently; daemon workers do not keep the supervisor alive.
            for instance in self.instances.values():
                instance.stop_child()


def main():
    parser = argparse.ArgumentParser(prog="inference-supervisor", description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--show-commands", action="store_true",
                        help="print instance commands/environment overrides; start nothing")
    parser.add_argument("--stop", action="store_true", help="stop configured owned Docker containers; stop supervision first")
    parser.add_argument("--mode", choices=("local", "server"), default="local",
                        help="local stops containers started here; server leaves them running")
    args = parser.parse_args()
    cfg = load_machine(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    supervisor = Supervisor(cfg, mode=args.mode)
    if args.stop:
        for instance in supervisor.instances.values():
            if hasattr(instance, "stop_container"):
                instance.stop_container()
        return
    if args.show_commands:
        for name, instance in supervisor.instances.items():
            env, _ = server_env(cfg, instance.server)
            overrides = {} if isinstance(instance, DockerInstance) else {
                k: v for k, v in env.items() if os.environ.get(k) != v}
            print(json.dumps({"instance": name, "argv": instance.command(check_files=False), "env": overrides}))
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: supervisor.stopping.set())
    supervisor.run()


if __name__ == "__main__":
    main()
