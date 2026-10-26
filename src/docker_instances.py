"""Docker-backed inference instances managed by inference-supervisor."""

import hashlib
import json
import logging
import subprocess

from common import config_path, download, model_files
from configuration import merge
from run_servers import register_voices

log = logging.getLogger("inference-supervisor")
OWNER = "io.audio-translation-dashboard.owner"
INSTANCE = "io.audio-translation-dashboard.instance"
SPEC = "io.audio-translation-dashboard.config"


class DockerMixin:
    # A host-specific wrapper can preserve its existing container identities.
    owner_label = OWNER
    instance_label = INSTANCE
    config_label = SPEC
    container_prefix = "atd-"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.docker = self.server["docker"]
        self.container = self.docker.get("name", self.container_prefix + self.name)
        self.owner = self.cfg.get("supervisor", {}).get("id", "inference-supervisor")
        self.started_here = False

    def cli(self, *args, missing=False):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            if missing and "No such" in result.stderr:
                return None
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def inspect(self):
        raw = self.cli("container", "inspect", self.container, missing=True)
        return json.loads(raw)[0] if raw is not None else None

    def owned(self, current):
        labels = current["Config"].get("Labels") or {}
        return labels.get(self.owner_label) == self.owner and labels.get(self.instance_label) == self.name

    def files(self):
        paths = merge(self.cfg.get("supervisor", {}).get("model_paths", {}).get(self.service, {}),
                      self.server.get("model_paths", {}))
        return [(flag, url, config_path(self.cfg, paths[flag[2:]]) if flag[2:] in paths else path)
                for flag, url, path in model_files(self.cfg, self.service)]

    def specification(self):
        files = self.files()
        command = [a for flag, _, path in files for a in (flag, "/models/" + path.name)]
        port = self.docker.get("port", 8080)
        command += ["--host", "0.0.0.0", "--port", str(port), *self.server.get("args", [])]
        spec = {"image": self.docker["image"], "gpu": str(self.server["gpu"]),
                "publish": f"{self.server['host']}:{self.server['port']}:{port}",
                "mounts": [{"source": str(path), "target": "/models/" + path.name} for _, _, path in files],
                "command": self.docker.get("command", command),
                "env": self.docker.get("env", {}),
                "restart": self.docker.get("restart", "no" if self.mode == "local" else "unless-stopped")}
        if "entrypoint" in self.docker:
            spec["entrypoint"] = self.docker["entrypoint"]
        return spec

    def command(self, check_files=True):
        spec = self.specification()
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        args = ["docker", "run", "-d", "--name", self.container,
                "--label", self.owner_label + "=" + self.owner, "--label", self.instance_label + "=" + self.name,
                "--label", self.config_label + "=" + digest, "--restart", spec["restart"],
                "--gpus", "device=" + spec["gpu"], "--publish", spec["publish"]]
        for mount in spec["mounts"]:
            args += ["--mount", f"type=bind,src={mount['source']},dst={mount['target']},readonly"]
        for key, value in spec["env"].items():
            args += ["--env", key + "=" + str(value)]
        if "entrypoint" in spec:
            args += ["--entrypoint", spec["entrypoint"]]
        return [*args, spec["image"], *spec["command"]]

    def prepare(self):
        with self.voice_lock:
            for _, url, path in self.files():
                if self.stopping.is_set():
                    return
                if not path.is_file():
                    log.info("%s: downloading model to %s", self.name, path)
                    download(url, path, resume=True)
        image = self.docker["image"]
        if self.cli("image", "inspect", image, missing=True) is None:
            log.info("%s: pulling %s; output: %s", self.name, image, self.log_path)
            with self.log_path.open("ab") as output:
                subprocess.run(["docker", "pull", image], stdout=output, stderr=subprocess.STDOUT,
                               timeout=900, check=True)

    def reconcile(self):
        self.prepare()
        if self.stopping.is_set():
            return
        argv = self.command()
        desired = next(arg[len(self.config_label)+1:] for arg in argv if arg.startswith(self.config_label + "="))
        current = self.inspect()
        with self.process_lock:
            if self.stopping.is_set():
                return
            if current is not None:
                if not self.owned(current):
                    raise RuntimeError(f"container {self.container} is not owned by this supervisor/instance")
                if (current["Config"].get("Labels") or {}).get(self.config_label) != desired:
                    if self.mode == "local" and current["State"]["Running"] and not self.started_here:
                        raise RuntimeError(f"stop {self.container} explicitly before changing its configuration")
                    log.info("%s: replacing container because configured settings changed", self.name)
                    self.cli("stop", "--time", str(self.grace), self.container)
                    self.cli("rm", self.container)
                    current = None
            if current is None:
                self.cli(*argv[1:])
                self.started_here = True
                log.info("%s: created %s", self.name, self.container)
            elif not current["State"]["Running"]:
                self.cli("start", self.container)
                self.started_here = True
                log.info("%s: started existing container", self.name)

    def poll(self):
        current = self.inspect()
        self.returncode = current["State"]["ExitCode"] if current else 1
        return None if current and current["State"]["Running"] else self.returncode

    def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    if not all(self.instances[n].ready.is_set() for n in self.settings.get("after", [])):
                        self.stopping.wait(0.2)
                        continue
                    self.reconcile()
                    if self.stopping.is_set():
                        break
                    started_at = self.inspect()["State"]["StartedAt"]
                    if not self.wait_ready(self):
                        break
                    with self.voice_lock:
                        if self.stopping.is_set():
                            break
                        register_voices(self.cfg, self.name, self.server, log)
                    if self.stopping.is_set():
                        break
                    self.ready.set()
                    log.info("%s: ready (%s); logs: docker logs -f %s", self.name, self.container, self.container)
                    while not self.stopping.wait(2):
                        current = self.inspect()
                        if (not current or not current["State"]["Running"]
                                or current["State"]["StartedAt"] != started_at):
                            raise RuntimeError("container exited, restarted or was removed")
                except (Exception, SystemExit) as error:
                    self.ready.clear()
                    log.warning("%s: %s; retry in %ss", self.name, error, self.retry)
                    self.stopping.wait(self.retry)
        finally:
            self.ready.clear()
            self.stop_child()
            log.info("%s: supervisor stopped", self.name)

    def stop_child(self):
        with self.process_lock:
            if self.mode == "local" and self.started_here:
                self.stop_container()
                self.started_here = False

    def stop_container(self):
        current = self.inspect()
        if current and self.owned(current):
            self.cli("stop", "--time", str(self.grace), self.container)
        elif current:
            raise RuntimeError(f"refusing to stop unowned container {self.container}")
