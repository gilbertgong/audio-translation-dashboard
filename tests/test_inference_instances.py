"""Supervisor lifecycle checks using scratch HTTP children, no models or GPUs."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "tmp"
WORK.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
from inference_instances import Supervisor, load_machine
from run_servers import server_env


FAKE = '''import os, pathlib, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
folder, name, port, delay, fail = sys.argv[1:]
folder = pathlib.Path(folder)
with (folder / (name + '.pids')).open('a') as f: f.write(str(os.getpid()) + '\\n')
if fail == 'yes' and not (folder / (name + '.failed')).exists():
    (folder / (name + '.failed')).touch()
    sys.exit(7)
time.sleep(float(delay))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', int(port)), Handler)
if name == 'whisper1':
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
    (folder / 'grandchild.pid').write_text(str(child.pid))
(folder / (name + '.ready')).write_text(str(time.monotonic()))
print(os.environ.get('CUDA_VISIBLE_DEVICES'), flush=True)
server.serve_forever()
'''


def wait_for(check, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for scratch child")


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        return state != "Z"
    except FileNotFoundError:
        return False


class SupervisorTests(unittest.TestCase):
    def test_local_reuses_existing_native_endpoint_without_starting_or_stopping_it(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            cfg = load_machine(ROOT / 'config/machines/examples/two-gpu-24gb.toml')
            cfg['supervisor']['log_dir'] = folder
            inst = Supervisor(cfg).instances['llama_gpu0']
            inst.settings['after'] = []
            inst.endpoint_ready = Mock(return_value=True)
            inst.stopping.wait = Mock()
            with patch('inference_instances.subprocess.Popen') as spawn, \
                    patch('inference_instances.stop_group') as stop:
                inst.run()
            spawn.assert_not_called()
            stop.assert_not_called()

    def test_local_combined_example_has_three_services_and_public_ports(self):
        cfg = load_machine(ROOT / 'config/machines/examples/linux-nvidia-24gb.toml')
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            cfg['supervisor']['log_dir'] = folder
            supervisor = Supervisor(cfg)
            self.assertEqual(set(supervisor.instances), {'whisper', 'llama', 'qwentts'})
            for service, port in [('whisper', 8081), ('llama', 8082), ('qwentts', 8085)]:
                inst = supervisor.instances[service]
                self.assertEqual(inst.server['port'], port)
                self.assertEqual(inst.specification()['restart'], 'no')
                self.assertEqual(inst.specification()['publish'], f'127.0.0.1:{port}:8080')
            self.assertEqual(supervisor.instances['llama'].settings['after'], ['whisper', 'qwentts'])

    def test_two_gpu_recipe_uses_shared_models_and_distinct_instances(self):
        cfg = load_machine(ROOT / "config/machines/examples/two-gpu-24gb.toml")
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            cfg["supervisor"]["log_dir"] = folder
            supervisor = Supervisor(cfg)
            self.assertEqual(len(supervisor.instances), 6)
            for gpu in range(2):
                for service, base in [("whisper", 8081), ("llama", 8082), ("qwentts", 8085)]:
                    instance = supervisor.instances[f"{service}_gpu{gpu}"]
                    self.assertEqual(instance.server["port"], base + gpu * 100)
                    env, _ = server_env(cfg, instance.server)
                    self.assertEqual(env["CUDA_VISIBLE_DEVICES"], str(gpu))
                    if service == "qwentts":
                        self.assertEqual(env["GGML_VK_VISIBLE_DEVICES"], str(gpu))
                    command = instance.command(check_files=False)
                    self.assertIn(str(base + gpu * 100), command)
                    if service == "llama":
                        self.assertIn("8192", command)
                    other = supervisor.instances[f"{service}_gpu{1-gpu}"].command(check_files=False)
                    self.assertEqual(command[command.index("--model") + 1],
                                     other[other.index("--model") + 1])
            self.assertEqual(cfg["models"]["whisper"]["model"], "large-v3")
            self.assertIn("1.7b", cfg["models"]["qwentts"]["model"])
            self.assertIn("codec", cfg["models"]["qwentts"])

    def test_machine_overrides_and_external_models(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            path = Path(folder) / "machine.toml"
            path.write_text('''[supervisor]
recipe = "nvidia-24gb"
[supervisor.model_paths.llama]
model = "/shared/model.gguf"
[api_servers.llama]
executable = "/native/llama-server"
[instances.custom]
service = "llama"
gpu = "GPU-example"
port = 9000
args = ["--ctx-size", "4096"]
''')
            cfg = load_machine(path)
            cfg["supervisor"]["log_dir"] = folder
            instance = Supervisor(cfg).instances["custom"]
            argv = instance.command(check_files=False)
            self.assertTrue(Path(argv[0]).as_posix().endswith("/native/llama-server"))
            self.assertTrue(any(Path(arg).as_posix().endswith("/shared/model.gguf") for arg in argv))
            self.assertEqual(argv[-2:], ["--ctx-size", "4096"])
            self.assertEqual(server_env(cfg, instance.server)[0]["CUDA_VISIBLE_DEVICES"], "GPU-example")

    @unittest.skipUnless(os.name == "posix" and Path('/proc').exists(), "Linux process groups")
    def test_restart_independence_startup_order_and_signal_cleanup(self):
        for stop_signal in [signal.SIGINT, signal.SIGTERM]:
            with self.subTest(signal=stop_signal), tempfile.TemporaryDirectory(dir=WORK) as folder:
                folder = Path(folder)
                fake = folder / "child.py"
                fake.write_text(FAKE)
                config = '[supervisor]\nrestart_delay_s = 0.2\nstop_timeout_s = 0.3\nlog_dir = ' + json.dumps(str(folder / 'logs')) + '\n'
                config += '[api_servers.fake]\nhost = "127.0.0.1"\nstartup_timeout_s = 5\n'
                names = []
                for gpu in range(2):
                    for service in ["whisper", "qwentts", "llama"]:
                        name = service + str(gpu)
                        names.append(name)
                        with socket.socket() as sock:
                            sock.bind(('127.0.0.1', 0))
                            port = sock.getsockname()[1]
                        command = [sys.executable, str(fake), str(folder), name, str(port),
                                   '1' if name == 'whisper0' else '0',
                                   'yes' if name == 'whisper0' else 'no']
                        config += f'\n[instances.{name}]\nservice = "fake"\ngpu = "{gpu}"\nport = {port}\ncommand = {json.dumps(command)}\n'
                        if service == 'llama':
                            config += f'after = ["whisper{gpu}", "qwentts{gpu}"]\n'
                path = folder / "machine.toml"
                path.write_text(config)
                with (folder / "supervisor.log").open('w') as output:
                    proc = subprocess.Popen([sys.executable, str(ROOT / 'src/inference_instances.py'), '--config', str(path)],
                                            stdout=output, stderr=subprocess.STDOUT)
                    try:
                        wait_for(lambda: all((folder / (n + '.ready')).exists() for n in names))
                        self.assertIsNone(proc.poll())
                        self.assertGreaterEqual(len((folder / 'whisper0.pids').read_text().splitlines()), 2)
                        ready = {n: float((folder / (n + '.ready')).read_text()) for n in names}
                        self.assertLess(ready['llama1'], ready['whisper0'])
                        for gpu in range(2):
                            self.assertGreaterEqual(ready['llama'+str(gpu)], ready['whisper'+str(gpu)])
                            self.assertGreaterEqual(ready['llama'+str(gpu)], ready['qwentts'+str(gpu)])
                        old_grandchild = int((folder / 'grandchild.pid').read_text())
                        old_pid = int((folder / 'whisper1.pids').read_text().splitlines()[-1])
                        os.kill(old_pid, signal.SIGTERM)
                        wait_for(lambda: len((folder / 'whisper1.pids').read_text().splitlines()) >= 2)
                        wait_for(lambda: int((folder / 'grandchild.pid').read_text()) != old_grandchild)
                        wait_for(lambda: not alive(old_grandchild))
                        self.assertEqual(len((folder / 'llama1.pids').read_text().splitlines()), 1)
                        proc.send_signal(stop_signal)
                        self.assertEqual(proc.wait(timeout=10), 0)
                        pids = [int(pid) for f in folder.glob('*.pids') for pid in f.read_text().splitlines()]
                        pids.append(int((folder / 'grandchild.pid').read_text()))
                        wait_for(lambda: not any(alive(pid) for pid in pids))
                    finally:
                        if proc.poll() is None:
                            proc.terminate()
                            proc.wait(timeout=10)
                        for f in list(folder.glob('*.pids')) + list(folder.glob('grandchild.pid')):
                            for pid in f.read_text().splitlines():
                                if alive(int(pid)):
                                    os.kill(int(pid), signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
