"""Exercise rendered policy permissions without accessing a cluster."""
import ipaddress
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def matches(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get('matchLabels', {}).items()):
        return False
    for expr in selector.get('matchExpressions', []):
        key, op, values = expr['key'], expr['operator'], expr.get('values', [])
        if op == 'In' and labels.get(key) not in values:
            return False
        if op == 'NotIn' and labels.get(key) in values:
            return False
        if op == 'Exists' and key not in labels:
            return False
        if op == 'DoesNotExist' and key in labels:
            return False
    return True


@unittest.skipUnless(shutil.which('helm'), 'Helm is required for network policy checks')
class NetworkPolicyTests(unittest.TestCase):
    def render(self, values, *flags, check=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'values.yaml'
            path.write_text(yaml.safe_dump(values))
            result = subprocess.run(['helm', 'template', 'test', str(ROOT), '--namespace',
                                     'custom-game', '-f', str(path), *flags],
                                    capture_output=True, text=True, check=check)
        if not check:
            return result
        return [obj for obj in yaml.safe_load_all(result.stdout)
                if obj and obj['kind'] == 'NetworkPolicy']

    def setUp(self):
        self.values = {'networkPolicy': {
            'enabled': True, 'nodeCIDRs': ['192.0.2.10/32'],
            'playerCIDRs': ['198.51.100.0/24', '2001:db8::/32'],
            'apiServerCIDRs': ['192.0.2.20/32'],
            'monitoring': {'enabled': True, 'podSelector': {'matchLabels': {'app': 'prometheus'}}},
        }}

    def allowed(self, policies, target, port, protocol='TCP', *, ip='203.0.113.1',
                namespace='other', labels=None):
        """Evaluate ingress rules for ordinary pod-network endpoints, not hostNetwork."""
        for policy in policies:
            if not matches(policy['spec']['podSelector'], target):
                continue
            for rule in policy['spec'].get('ingress', []):
                ports = rule.get('ports', [])
                if ports and not any(
                    entry.get('protocol', 'TCP') == protocol and (
                        entry['port'] == port or
                        isinstance(entry['port'], int) and isinstance(port, int) and
                        entry['port'] <= port <= entry.get('endPort', entry['port'])
                    ) for entry in ports
                ):
                    continue
                for peer in rule.get('from', []):
                    if 'ipBlock' in peer:
                        if ipaddress.ip_address(ip) in ipaddress.ip_network(peer['ipBlock']['cidr']):
                            return True
                    else:
                        ns = peer.get('namespaceSelector')
                        if ns is None and namespace != 'custom-game':
                            continue
                        if ns is not None and not matches(ns, {'kubernetes.io/metadata.name': namespace}):
                            continue
                        if matches(peer.get('podSelector', {}), labels or {}):
                            return True
        return False

    def test_disabled_by_default(self):
        self.assertEqual(self.render({}), [])

    def test_ingress_boundaries(self):
        policies = self.render(self.values)
        self.assertEqual(len(policies), 7)
        for policy in policies:
            self.assertEqual(policy['metadata']['namespace'], 'custom-game')
            self.assertEqual(policy['spec']['policyTypes'], ['Ingress'])
            self.assertNotIn('egress', policy['spec'])
        targets = [{}, {'role': 'igw-database'}, {'role': 'igw-message-queue', 'messagequeue': 'admin'}]
        for target in targets:
            self.assertTrue(self.allowed(policies, target, 15432, namespace='custom-game'))
            self.assertTrue(self.allowed(policies, target, 15432, ip='192.0.2.10'))
            self.assertFalse(self.allowed(policies, target, 15432))
            self.assertFalse(self.allowed(policies, target, 15432, ip='198.51.100.5'))
        # No chart allow policy targets the unauthenticated browser.
        for source in ({'namespace': 'custom-game'}, {'ip': '192.0.2.10'},
                       {'namespace': 'monitoring', 'labels': {'app': 'prometheus'}}):
            self.assertFalse(self.allowed(policies, {'role': 'igw-filebrowser'}, 18888, **source))
        game = {'role': 'igw-server'}
        for port in (7777, 7790, 7810):
            self.assertTrue(self.allowed(policies, game, port, 'UDP', ip='198.51.100.5'))
            self.assertTrue(self.allowed(policies, game, port, 'UDP', ip='2001:db8::5'))
        self.assertFalse(self.allowed(policies, game, 7811, 'UDP', ip='198.51.100.5'))
        self.assertFalse(self.allowed(policies, game, 7777, 'TCP', ip='198.51.100.5'))
        self.assertFalse(self.allowed(policies, game, 7777, 'UDP'))
        queue = {'role': 'igw-message-queue', 'messagequeue': 'game'}
        self.assertTrue(self.allowed(policies, queue, 'amqp', ip='198.51.100.5'))
        self.assertFalse(self.allowed(policies, queue, 31982, ip='198.51.100.5'))
        self.assertFalse(self.allowed(policies, dict(queue, messagequeue='admin'), 'amqp', ip='198.51.100.5'))
        self.assertFalse(self.allowed(policies, queue, 15672, ip='198.51.100.5'))
        for operator in ('battlegroup', 'database', 'server', 'utilities'):
            target = {'control-plane': operator + '-controller-manager'}
            self.assertTrue(self.allowed(policies, target, 9443, ip='192.0.2.20'))
            self.assertFalse(self.allowed(policies, target, 8081, ip='192.0.2.20'))
            self.assertFalse(self.allowed(policies, target, 9443))
        self.assertTrue(self.allowed(policies, queue, 15692, namespace='monitoring', labels={'app': 'prometheus'}))
        self.assertFalse(self.allowed(policies, queue, 15692, namespace='monitoring', labels={'app': 'untrusted'}))
        self.assertFalse(self.allowed(policies, queue, 15692, namespace='other', labels={'app': 'prometheus'}))

    def test_monitoring_is_opt_in(self):
        self.values['networkPolicy']['monitoring']['enabled'] = False
        policies = self.render(self.values)
        self.assertEqual(len(policies), 6)
        self.assertFalse(self.allowed(policies, {}, 15692, namespace='monitoring'))

    def test_invalid_settings_fail_before_install(self):
        invalid = [
            {'enabled': True},
            {'enabled': True, 'nodeCIDRs': ['192.0.2.10/32'], 'playerCIDRs': []},
            {'enabled': True, 'nodeCIDRs': ['192.0.2.10/32'], 'apiServerCIDRs': []},
            {'enabled': True, 'nodeCIDRs': ['not-a-cidr']},
            {'enabled': True, 'nodeCIDRs': ['192.0.2.10/32'], 'game': {'port': 7810, 'endPort': 7777}},
            {'enabled': True, 'nodeCIDRs': ['192.0.2.10/32'], 'game': {'port': 0}},
        ]
        for values in invalid:
            with self.subTest(values=values):
                self.assertNotEqual(self.render({'networkPolicy': values}, check=False).returncode, 0)
        self.assertNotEqual(self.render(self.values, '--kube-version', '1.24.0', check=False).returncode, 0)
