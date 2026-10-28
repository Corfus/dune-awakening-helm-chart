"""Credential preservation and packaging regressions; no cluster access."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_module('bootstrap', 'files/apply-battlegroup.py')
helper = load_module('credentials', 'scripts/create-credentials.py')


class CredentialTests(unittest.TestCase):
    def test_runtime_password_and_persistent_gateway_key(self):
        template = {'metadata': {}, 'spec': {'serverGroup': {'template': {'spec': {
            'global': {'userIniConfig': {'files': {
                'UserEngine.ini': '[ConsoleVariables]\nBgd.ServerLoginPassword=""\n'
            }}}
        }}}}}
        original = copy.deepcopy(template)
        credentials = {
            'fls-token': 'test-token', 'database-password': 'test-db',
            'postgres-password': 'test-super', 'world-id': 'sh-test-abcdef',
            'rmq-auth-secret': 'test-rmq', 'fls-apikey': 'persistent-test-key',
            'server-login-password': 'private player password',
        }
        gateway, _, world = bootstrap.render(template, credentials, 'test', '192.0.2.1')
        self.assertEqual(base64.b64decode(gateway['data']['fls-apikey']).decode(),
                         credentials['fls-apikey'])
        server = world['spec']['serverGroup']['template']['spec']['global']
        self.assertIn('Bgd.ServerLoginPassword="private player password"',
                      server['userIniConfig']['files']['UserEngine.ini'])
        self.assertEqual(server['additionalArguments'], [
            '-ini:engine:[ConsoleVariables]:Bgd.ServerLoginPassword=private player password'])
        self.assertEqual(template, original)
        credentials['server-login-password'] = 'unsafe\ninjection'
        with self.assertRaises(ValueError):
            bootstrap.render(template, credentials, 'test', '192.0.2.1')

    def run_existing(self, data):
        existing = {'metadata': {'resourceVersion': '42'}, 'data': data}
        with patch.object(helper.sys, 'argv', ['create-credentials.py', '--namespace', 'test']), \
             patch.object(helper.subprocess, 'run') as run, \
             patch.object(helper.getpass, 'getpass') as prompt:
            run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(existing)),
                               subprocess.CompletedProcess([], 0, '')]
            helper.main()
            prompt.assert_not_called()
            return run.call_args_list

    def test_upgrade_adds_only_gateway_key_using_stdin(self):
        calls = self.run_existing({'database-password': 'preserved', 'world-id': 'preserved'})
        self.assertEqual(len(calls), 2)
        update = json.loads(calls[1].kwargs['input'])
        self.assertEqual(update['metadata'], {'resourceVersion': '42'})
        self.assertEqual(set(update['data']), {'fls-apikey'})
        self.assertNotIn(update['data']['fls-apikey'], ' '.join(calls[1].args[0]))

    def test_existing_gateway_key_is_not_rotated(self):
        self.assertEqual(len(self.run_existing({'fls-apikey': 'preserved'})), 1)


@unittest.skipUnless(shutil.which('helm'), 'Helm is required for packaging checks')
class PackageTests(unittest.TestCase):
    def test_private_files_excluded_and_plaintext_values_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / 'chart'
            shutil.copytree(ROOT, chart, ignore=shutil.ignore_patterns('.git', 'dist', '.idea', '__pycache__'))
            private_files = ['values.private.yaml', 'values.private.prod.yml',
                             'secret.yaml', 'secrets.yml', 'tls.key', '.env']
            for name in private_files:
                (chart / name).write_text('test-sensitive-marker')
            subprocess.run(['helm', 'package', str(chart), '--destination', str(root)],
                           check=True, capture_output=True)
            with tarfile.open(next(root.glob('*.tgz'))) as archive:
                names = {name.split('/', 1)[1] for name in archive.getnames() if '/' in name}
                self.assertFalse(names.intersection(private_files))
                self.assertFalse(any(name.startswith('.github/') for name in names))
            result = subprocess.run(['helm', 'template', 'test', str(chart),
                                     '--set-string', 'world.serverLoginPassword=test-password'],
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
