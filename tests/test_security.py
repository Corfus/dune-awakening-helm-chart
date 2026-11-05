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

import yaml


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

    def test_backup_endpoint_uses_service_ip_for_path_style(self):
        for address, endpoint in [('10.43.1.2', 'http://10.43.1.2:8333'),
                                  ('fd00::1234', 'http://[fd00::1234]:8333')]:
            config = bootstrap.backup_config('dune', address, 'dune-backups')
            data = {key: base64.b64decode(value).decode() for key, value in config['data'].items()}
            self.assertEqual(data['AWS_ENDPOINT_URL'], endpoint)
            self.assertEqual(data['BARMAN_CLOUD_BUCKET_NAME'], 'dune-backups')
            self.assertNotIn('AWS_SECRET_ACCESS_KEY', data)
            self.assertEqual(config['metadata']['labels'], {'igw.funcom.com/backup-config': 'dune'})
        for address in ('', 'None', 'dune-backups.dune.svc'):
            with self.assertRaises(ValueError):
                bootstrap.backup_config('dune', address, 'dune-backups')

    def run_existing(self, data):
        existing = {'metadata': {'resourceVersion': '42'}, 'data': data}
        with patch.object(helper.sys, 'argv', ['create-credentials.py', '--namespace', 'test']), \
             patch.object(helper.subprocess, 'run') as run, \
             patch.object(helper.getpass, 'getpass') as prompt, \
             patch.object(helper, 'backup_credentials'):
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


class BackupCredentialTests(unittest.TestCase):
    def existing(self, labels=None):
        return {'metadata': {'resourceVersion': '42', 'labels': labels or {}},
                'data': {key: base64.b64encode(value).decode() for key, value in
                         [('AWS_ACCESS_KEY_ID', b'existing-access'),
                          ('AWS_SECRET_ACCESS_KEY', b'existing-secret')]}}

    def run_helper(self, existing=None):
        with patch.object(helper.subprocess, 'run') as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(existing) if existing else ''),
                               subprocess.CompletedProcess([], 0, '')]
            secret = helper.backup_credentials('test', 'custom-backups')
            return secret, run.call_args_list

    def test_create_random_keys_and_discovery_label_using_stdin(self):
        secret, calls = self.run_helper()
        self.assertEqual(secret['metadata'], {'name': 'custom-backups', 'namespace': 'test',
                         'labels': {'igw.funcom.com/backup-credentials': 'custom-backups'}})
        self.assertEqual(json.loads(calls[1].kwargs['input']), secret)
        values = [base64.b64decode(v) for v in secret['data'].values()]
        self.assertTrue(all(len(v) >= 32 for v in values))
        self.assertNotEqual(*values)
        for value in values:
            self.assertNotIn(value.decode(), ' '.join(calls[1].args[0]))

    def test_existing_keys_preserved_and_label_repaired(self):
        existing = self.existing({'keep': 'me'})
        secret, calls = self.run_helper(existing)
        self.assertEqual(secret['data'], existing['data'])
        self.assertEqual(secret['metadata']['labels']['keep'], 'me')
        self.assertEqual(json.loads(calls[1].kwargs['input']), {'metadata': {
            'resourceVersion': '42', 'labels': {'igw.funcom.com/backup-credentials': 'custom-backups'}}})

    def test_labeled_secret_is_not_modified(self):
        existing = self.existing({'igw.funcom.com/backup-credentials': 'custom-backups'})
        secret, calls = self.run_helper(existing)
        self.assertEqual(secret['data'], existing['data'])
        self.assertEqual(len(calls), 1)

    def test_incomplete_existing_credentials_are_not_rotated(self):
        for value in ('', '%%%'):
            existing = self.existing()
            existing['data']['AWS_SECRET_ACCESS_KEY'] = value
            with patch.object(helper.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, json.dumps(existing))) as run:
                with self.assertRaises(RuntimeError):
                    helper.backup_credentials('test', 'custom-backups')
                self.assertEqual(run.call_count, 1)

    def test_sealed_output_contains_both_secrets_and_backup_label(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sealed.yaml'
            backup = {'metadata': {'labels': {'igw.funcom.com/backup-credentials': 'custom-backups'}}}
            with patch.object(helper.sys, 'argv', ['create-credentials.py', '--sealed-output', str(output),
                                                  '--backup-secret-name', 'custom-backups']), \
                 patch.object(helper, 'backup_credentials', return_value=backup) as get_backup, \
                 patch.object(helper.subprocess, 'run') as run:
                run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps({'data': {'fls-apikey': 'preserved'}})),
                                   subprocess.CompletedProcess([], 0, 'kind: SealedSecret\n'),
                                   subprocess.CompletedProcess([], 0, 'kind: SealedSecret\n')]
                helper.main()
                get_backup.assert_called_once_with('dune', 'custom-backups')
                self.assertEqual(json.loads(run.call_args_list[2].kwargs['input']), backup)
                self.assertEqual(len(list(yaml.safe_load_all(output.read_text()))), 2)


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
