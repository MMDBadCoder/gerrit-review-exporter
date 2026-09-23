"""Shared config contract for both standalone helpers; no network required."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = []
for name in ('review', 'implement'):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'skills/gerrit-{name}/gerrit_{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    HELPERS.append(module)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.env = patch.dict(os.environ, {'HOME': str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, **values):
        self.config.write_text(json.dumps(values))

    def parse(self, helper, *extra):
        return helper.parser().parse_args(['--config', str(self.config), *extra, 'doctor'])

    def test_inline_credential(self):
        self.write(url='https://example.com', username='bot', http_password='secret')
        for h in HELPERS:
            c = h.Client(self.parse(h))
            self.assertEqual(c.secret, 'secret')
            self.assertEqual(c.username, 'bot')

    def test_precedence(self):
        os.environ.update(GERRIT_URL='https://environment.example', GERRIT_HTTP_PASSWORD='environment')
        self.write(url='https://config.example', username='bot', http_password='config', timeout=20)
        for h in HELPERS:
            args = self.parse(h, '--url', 'https://cli.example', '--timeout', '5')
            self.assertEqual(args.url, 'https://cli.example')
            self.assertEqual(args.timeout, 5)
            self.assertEqual(h.Client(args).secret, 'config')

    def test_relative_paths(self):
        self.write(ca_file='certs/ca.pem', credential_file='password')
        for h in HELPERS:
            args = self.parse(h)
            self.assertEqual(args.ca_file, str(self.root / 'certs/ca.pem'))
            self.assertEqual(args.credential_file, str(self.root / 'password'))

    def test_file_override(self):
        (self.root / 'password').write_text('file-secret\n')
        self.write(url='https://example.com', username='bot', http_password='inline')
        for h in HELPERS:
            c = h.Client(self.parse(h, '--credential-file', str(self.root / 'password')))
            self.assertEqual(c.secret, 'file-secret')

    def test_default_discovery(self):
        default = self.root / '.config/gerrit-agent/config.json'
        default.parent.mkdir(parents=True)
        default.write_text('{"url":"https://default.example"}')
        for h in HELPERS:
            self.assertEqual(h.parser().parse_args(['doctor']).url, 'https://default.example')

    def test_environment_discovery(self):
        self.write(url='https://selected.example')
        os.environ['GERRIT_CONFIG'] = str(self.config)
        for h in HELPERS:
            self.assertEqual(h.parser().parse_args(['doctor']).url, 'https://selected.example')

    def test_errors_redact_contents(self):
        for raw in ('{"http_password":"secret",', '["secret"]', '{"secret":"value"}', '{"timeout":0}', '{"auth":"secret"}', '{"username":null}'):
            self.config.write_text(raw)
            for h in HELPERS:
                with self.assertRaises(h.ReviewError) as caught:
                    self.parse(h)
                self.assertNotIn('secret', str(caught.exception))

    def test_missing_explicit_and_environment_fallback(self):
        os.environ.update(GERRIT_URL='https://env.example', GERRIT_USER='bot', GERRIT_HTTP_PASSWORD='old-way')
        for h in HELPERS:
            with self.assertRaises(h.ReviewError):
                self.parse(h)
            c = h.Client(h.parser().parse_args(['doctor']))
            self.assertEqual(c.secret, 'old-way')
            self.assertEqual(c.base, 'https://env.example')


if __name__ == '__main__':
    unittest.main()
