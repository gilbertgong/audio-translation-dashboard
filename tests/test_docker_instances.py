"""Docker reconciliation checks without a Docker daemon or GPUs."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch, call

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "tmp"
WORK.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / 'src'))
from inference_instances import DockerInstance, Supervisor, load_machine
from docker_instances import OWNER, INSTANCE, SPEC


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.folder.cleanup)
        cfg = load_machine(ROOT / 'config/machines/examples/llama-docker.toml')
        cfg['supervisor']['log_dir'] = self.folder.name
        self.instance = Supervisor(cfg, mode="server").instances['llama_gpu1']
        self.instance.prepare = Mock()
        self.instance.cli = Mock()
        argv = self.instance.command()
        digest = next(a.split('=', 1)[1] for a in argv if a.startswith(SPEC + '='))
        self.current = {'Config': {'Labels': {
            OWNER: self.instance.owner, INSTANCE: self.instance.name, SPEC: digest}},
            'State': {'Running': True, 'ExitCode': 0}}

    def test_create_reuse_and_start_stopped(self):
        inst = self.instance
        inst.inspect = Mock(return_value=None)
        inst.reconcile()
        self.assertEqual(inst.cli.call_args.args, tuple(inst.command()[1:]))
        inst.cli.reset_mock()
        inst.inspect.return_value = self.current
        inst.reconcile()
        inst.cli.assert_not_called()
        self.current['State']['Running'] = False
        inst.reconcile()
        inst.cli.assert_called_once_with('start', inst.container)

    def test_configuration_change_replaces_owned_container(self):
        inst = self.instance
        inst.inspect = Mock(return_value=self.current)
        inst.server['args'] = ['--ctx-size', '4096']
        inst.reconcile()
        self.assertEqual(inst.cli.call_args_list[0].args, ('stop', '--time', '10', inst.container))
        self.assertEqual(inst.cli.call_args_list[1].args, ('rm', inst.container))
        self.assertEqual(inst.cli.call_args_list[2].args[0], 'run')

    def test_whisper_entrypoint_and_normal_model_location(self):
        cfg = load_machine(ROOT / 'config/machines/examples/whisper-docker.toml')
        cfg['supervisor']['log_dir'] = self.folder.name
        inst = Supervisor(cfg, mode="server").instances['whisper_gpu1']
        argv = inst.command()
        self.assertEqual(argv[argv.index('--entrypoint') + 1], 'whisper-server')
        self.assertEqual(inst.files()[0][2], ROOT / 'models/whisper/ggml-large-v3.bin')
        self.assertIn('--language', inst.specification()['command'])
        old = inst.specification()
        inst.docker['entrypoint'] = '/app/build/bin/whisper-server'
        self.assertNotEqual(inst.specification(), old)
        self.assertNotIn('--entrypoint', self.instance.command())

    def test_qwentts_uses_both_standard_model_files(self):
        cfg = load_machine(ROOT / 'config/machines/examples/qwentts-docker.toml')
        cfg['supervisor']['log_dir'] = self.folder.name
        inst = Supervisor(cfg, mode="server").instances['qwentts_gpu1']
        spec = inst.specification()
        self.assertEqual(spec['entrypoint'], '/app/tts-server')
        self.assertEqual(len(spec['mounts']), 2)
        self.assertTrue(all(Path(m['source']).parent == ROOT / 'models/qwentts'
                            for m in spec['mounts']))
        self.assertIn('--codec', spec['command'])
        self.assertIn('qwen_demo', inst.server['voices'])

    def test_container_restart_registers_voices_again_before_ready(self):
        inst = self.instance
        inst.retry = 0.01
        inst.reconcile = Mock()
        inst.wait_ready = Mock(return_value=True)
        current = copy.deepcopy(self.current)
        current['State']['StartedAt'] = 'first'
        inst.inspect = Mock(return_value=current)
        first, second = threading.Event(), threading.Event()
        calls = []

        def register(*args):
            self.assertFalse(inst.ready.is_set())
            calls.append(current['State']['StartedAt'])
            (first if len(calls) == 1 else second).set()

        with patch('docker_instances.register_voices', side_effect=register):
            inst.thread.start()
            try:
                self.assertTrue(first.wait(3))
                self.assertTrue(inst.ready.wait(3))
                current['State']['StartedAt'] = 'second'
                self.assertTrue(second.wait(5))
                self.assertEqual(calls, ['first', 'second'])
            finally:
                inst.stopping.set()
                inst.thread.join(timeout=3)
            self.assertFalse(inst.thread.is_alive())

    def test_unowned_container_is_never_replaced_or_stopped(self):
        inst = self.instance
        current = copy.deepcopy(self.current)
        current['Config']['Labels'][OWNER] = 'someone-else'
        inst.inspect = Mock(return_value=current)
        with self.assertRaisesRegex(RuntimeError, 'not owned'):
            inst.reconcile()
        with self.assertRaisesRegex(RuntimeError, 'unowned'):
            inst.stop_container()
        inst.cli.assert_not_called()

    def local_instance(self):
        cfg = load_machine(ROOT / 'config/machines/examples/llama-docker.toml')
        cfg['supervisor']['log_dir'] = self.folder.name
        inst = Supervisor(cfg).instances['llama_gpu1']
        inst.prepare = Mock()
        inst.cli = Mock()
        digest = next(a.split('=', 1)[1] for a in inst.command() if a.startswith(SPEC + '='))
        current = {'Config': {'Labels': {OWNER: inst.owner, INSTANCE: inst.name, SPEC: digest}},
                   'State': {'Running': True, 'ExitCode': 0}}
        inst.inspect = Mock(return_value=current)
        return inst, current

    def test_local_stops_only_containers_created_or_started_here(self):
        for initial in (None, False, True):
            with self.subTest(initial=initial):
                inst, current = self.local_instance()
                current['State']['Running'] = initial
                inst.inspect.return_value = None if initial is None else current
                inst.reconcile()
                self.assertEqual(inst.started_here, initial is not True)
                self.assertEqual(inst.specification()['restart'], 'no')
                inst.cli.reset_mock()
                current['State']['Running'] = True
                inst.inspect.return_value = current
                inst.stop_child()
                inst.stop_child()  # final cleanup is idempotent
                if initial is True:
                    inst.cli.assert_not_called()
                else:
                    inst.cli.assert_called_once_with('stop', '--time', '10', inst.container)
                self.assertFalse(inst.started_here)

    def test_server_leaves_even_newly_created_container_running(self):
        inst = self.instance
        inst.inspect = Mock(return_value=None)
        inst.reconcile()
        self.assertTrue(inst.started_here)
        self.assertEqual(inst.specification()['restart'], 'unless-stopped')
        inst.cli.reset_mock()
        inst.stop_child()
        inst.cli.assert_not_called()

    def test_wrapper_can_preserve_existing_container_names_and_labels(self):
        class ExistingDocker(DockerInstance):
            container_prefix = 'existing-'
            owner_label = 'example.owner'
            instance_label = 'example.instance'
            config_label = 'example.config'

        class ExistingSupervisor(Supervisor):
            docker_class = ExistingDocker

        base = self.instance
        inst = ExistingSupervisor(base.cfg, mode='server').instances[base.name]
        inst.prepare = Mock()
        inst.cli = Mock()
        argv = inst.command()
        digest = next(a.split('=', 1)[1] for a in argv if a.startswith('example.config='))
        inst.inspect = Mock(return_value={'Config': {'Labels': {
            'example.owner': inst.owner, 'example.instance': inst.name, 'example.config': digest}},
            'State': {'Running': True, 'ExitCode': 0}})
        inst.reconcile()
        self.assertEqual(inst.container, 'existing-' + base.name)
        inst.cli.assert_not_called()

    def test_local_does_not_replace_a_running_container_it_did_not_start(self):
        inst, current = self.local_instance()
        inst.server['args'] = ['--ctx-size', '4096']
        with self.assertRaisesRegex(RuntimeError, 'stop .* explicitly'):
            inst.reconcile()
        inst.cli.assert_not_called()

    def test_stopped_local_container_can_be_replaced_for_config_change(self):
        inst, current = self.local_instance()
        current['State']['Running'] = False
        inst.server['args'] = ['--ctx-size', '4096']
        inst.reconcile()
        self.assertTrue(inst.started_here)
        self.assertEqual(inst.cli.call_args_list[0], call('stop', '--time', '10', inst.container))
        self.assertEqual(inst.cli.call_args_list[1], call('rm', inst.container))

    def test_prepare_downloads_missing_standard_sources_then_reuses_cache(self):
        cfg = load_machine(ROOT / 'config/machines/examples/qwentts-docker.toml')
        cfg['supervisor']['log_dir'] = self.folder.name
        cfg['instances']['qwentts_gpu1']['model_paths'] = {
            'model': str(Path(self.folder.name) / 'talker.gguf'),
            'codec': str(Path(self.folder.name) / 'codec.gguf')}
        inst = Supervisor(cfg).instances['qwentts_gpu1']
        inst.cli = Mock(return_value=None)

        def download(url, path, resume):
            self.assertIn('huggingface.co', url)
            self.assertTrue(resume)
            path.write_bytes(b'mock model')

        with patch('docker_instances.download', side_effect=download) as fetch, \
                patch('docker_instances.subprocess.run') as run:
            inst.prepare()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(run.call_args.args[0], ['docker', 'pull', inst.docker['image']])
            fetch.reset_mock()
            run.reset_mock()
            inst.cli.return_value = 'cached image'
            inst.prepare()
            fetch.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
