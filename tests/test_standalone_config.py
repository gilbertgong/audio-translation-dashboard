"""Offline standalone app configuration checks; no servers start."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import configuration
import app


class StandaloneConfigTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.config = self.folder / 'complete config.toml'

    def test_standalone_reads_only_selected_file_and_keeps_project_path_base(self):
        self.config.write_text('[server]\nstart = false\n[api_servers.custom]\nport = 12345\n')
        with patch.object(configuration, 'layers', side_effect=AssertionError('Layered config consulted')):
            cfg = configuration.load(self.config, standalone=True)
        self.assertEqual(cfg, {'server': {'start': False}, 'api_servers': {'custom': {'port': 12345}},
                               '_config_dir': ROOT, '_files': [self.config.resolve()]})

    def test_normal_layering_is_unchanged(self):
        defaults, generated = self.folder / 'defaults.toml', self.folder / 'generated.toml'
        defaults.write_text('[server]\nhost = "default"\nport = 1\n')
        generated.write_text('[server]\nport = 2\n')
        self.config.write_text('[server]\nhost = "local"\n')
        with patch.object(configuration, 'DEFAULTS_FILE', defaults), \
                patch.object(configuration, 'GENERATED_FILE', generated):
            cfg = configuration.load(self.config)
        self.assertEqual(cfg['server'], {'host': 'local', 'port': 2})
        self.assertEqual(cfg['_files'], [defaults.resolve(), generated.resolve(), self.config.resolve()])

    def test_app_derives_backend_urls_from_the_standalone_file(self):
        self.config.write_text(configuration.DEFAULTS_FILE.read_text().replace(
            'host = "127.0.0.1"', 'host = "192.0.2.90"'))
        with patch.object(configuration, 'layers', side_effect=AssertionError('Layered config consulted')):
            cfg = app.load_config(self.config, standalone=True)
        self.assertEqual(cfg['whisper_url'], 'http://192.0.2.90:8081')
        self.assertEqual(cfg['_files'], [self.config.resolve()])
