"""Verify public backup settings reach the operator's database configuration."""
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('helm'), 'Helm is required')
class BackupTests(unittest.TestCase):
    def render(self, *args):
        return subprocess.run(
            ['helm', 'template', 'dune', str(ROOT), '--namespace', 'dune', *args],
            capture_output=True, text=True,
        )

    def database(self, *args):
        result = self.render(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = next(doc for doc in yaml.safe_load_all(result.stdout)
                      if doc and doc.get('kind') == 'ConfigMap'
                      and doc['metadata']['name'] == 'dune-battlegroup-template')
        group = yaml.safe_load(config['data']['battlegroup.yaml'])
        return group['spec']['database']['template']['spec']['deployment']['spec']

    def test_disabled_by_default(self):
        db = self.database()
        self.assertIs(db['enablePhysicalBackups'], False)
        self.assertNotIn('backupSchedule', db)

    def test_daily_and_custom_schedule(self):
        db = self.database('--set', 'backups.enabled=true')
        self.assertIs(db['enablePhysicalBackups'], True)
        self.assertEqual(db['backupSchedule'], '0 3 * * *')
        db = self.database('--set', 'backups.enabled=true',
                           '--set-string', 'backups.schedule=30 1 * * 0')
        self.assertEqual(db['backupSchedule'], '30 1 * * 0')

    def test_invalid_values_rejected(self):
        for option in ('backups.schedule=', 'backups.schedule=invalid',
                       'backups.enabled=yes', 'backups.unknown=true'):
            with self.subTest(option=option):
                self.assertNotEqual(self.render('--set-string', option).returncode, 0)

    def test_storage_and_secret_wiring(self):
        args = ('--set', 'backups.enabled=true', '--namespace', 'backup-test',
                '--set', 'backups.storage.class=backup-disks',
                '--set', 'backups.storage.size=200Gi',
                '--set', 'backups.existingSecret=my-backup-keys')
        result = self.render(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = {(d['kind'], d['metadata']['name']): d
                   for d in yaml.safe_load_all(result.stdout) if d}
        pvc = objects['PersistentVolumeClaim', 'dune-backups']
        self.assertEqual(pvc['spec']['storageClassName'], 'backup-disks')
        self.assertEqual(pvc['spec']['resources']['requests']['storage'], '200Gi')
        self.assertEqual(pvc['metadata']['annotations']['helm.sh/resource-policy'], 'keep')
        self.assertEqual(pvc['metadata']['annotations']['argocd.argoproj.io/sync-options'],
                         'Prune=false,Delete=false')
        pod = objects['Deployment', 'dune-backups']['spec']['template']['spec']
        self.assertEqual(pod['volumes'][0]['persistentVolumeClaim']['claimName'], 'dune-backups')
        container = pod['containers'][0]
        self.assertEqual(container['volumeMounts'][0]['mountPath'], '/data')
        self.assertIn('-dir=/data', container['args'])
        self.assertIn('-bucket=dune-backups', container['args'])
        for env in container['env']:
            self.assertEqual(env['valueFrom']['secretKeyRef']['name'], 'my-backup-keys')
        config = objects['Secret', 'dune-backup-config']['stringData']
        self.assertEqual(config['AWS_ENDPOINT_URL'], 'http://dune-backups.backup-test.svc:8333')
        self.assertEqual(config['BARMAN_CLOUD_BUCKET_NAME'], 'dune-backups')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', config)
        db = self.database(*args)
        for selectors in (db['envSecretSelectors'], db['utilityRuntimeConfig']['envSecretSelectors']):
            self.assertIn({'matchLabels': {'igw.funcom.com/backup-credentials': 'my-backup-keys'}}, selectors)
            self.assertIn({'matchLabels': {'igw.funcom.com/backup-config': 'dune'}}, selectors)

    def test_default_storage_class_and_disabled_resources(self):
        objects = list(yaml.safe_load_all(self.render('--set', 'backups.enabled=true').stdout))
        pvc = next(d for d in objects if d and d['kind'] == 'PersistentVolumeClaim')
        self.assertNotIn('storageClassName', pvc['spec'])
        objects = list(yaml.safe_load_all(self.render().stdout))
        self.assertFalse(any(d and d['metadata']['name'] in ('dune-backups', 'dune-backup-config')
                             for d in objects))

    def test_internal_storage_ports_blocked(self):
        from test_networkpolicies import NetworkPolicyTests
        checker = NetworkPolicyTests()
        for enabled in (False, True):
            result = self.render('--set', 'backups.enabled=true', '--namespace', 'custom-game',
                                 '--set', f'networkPolicy.enabled={str(enabled).lower()}',
                                 '--set-string', 'networkPolicy.nodeCIDRs[0]=192.0.2.10/32')
            self.assertEqual(result.returncode, 0, result.stderr)
            policies = [d for d in yaml.safe_load_all(result.stdout)
                        if d and d['kind'] == 'NetworkPolicy']
            target = {'app.kubernetes.io/name': 'dune-backups'}
            self.assertTrue(checker.allowed(policies, target, 8333, namespace='custom-game'))
            for port in (8888, 9333, 9340, 18888, 19333, 19340):
                for source in ({'namespace': 'custom-game'}, {'ip': '192.0.2.10'}, {}):
                    self.assertFalse(checker.allowed(policies, target, port, **source))
