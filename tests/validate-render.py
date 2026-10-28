#!/usr/bin/env python3
"""Validate a `helm template --include-crds` render (requires PyYAML)."""
import sys
import yaml

objects = list(yaml.safe_load_all(open(sys.argv[1])))
objects = [obj for obj in objects if obj]
indexed = {(obj['kind'], obj['metadata']['name']): obj for obj in objects}
assert len(indexed) == len(objects), 'Duplicate resources'
assert sum(obj['kind'] == 'CustomResourceDefinition' for obj in objects) == 26
assert sum(obj['kind'] == 'Deployment' for obj in objects) == 4
world = yaml.safe_load(indexed['ConfigMap', 'dune-battlegroup-template']['data']['battlegroup.yaml'])
assert world['kind'] == 'BattleGroup'
assert world['spec']['utilities']['fileBrowser']['spec']['suspend'] is True
assert 'Bgd.ServerLoginPassword=""' in world['spec']['serverGroup']['template']['spec']['global']['userIniConfig']['files']['UserEngine.ini']
assert '__DUNE_DATABASE_PASSWORD__' in str(world)
assert '__DUNE_FLS_TOKEN__' in str(world)
job = indexed['Job', 'dune-configure-battlegroup']
assert job['metadata']['annotations']['helm.sh/hook'] == 'post-install,post-upgrade'
assert job['metadata']['annotations']['argocd.argoproj.io/hook'] == 'Sync'
volumes = {v['name']: v for v in job['spec']['template']['spec']['volumes']}
for key in ('template', 'script'):
    assert ('ConfigMap', volumes[key]['configMap']['name']) in indexed
allowed = {
    'igw-k8s-battlegroup-operator', 'igw-k8s-database-operator',
    'igw-k8s-server-operator', 'igw-k8s-utilities-operator', 'igw-postgres',
    'seabass-server', 'seabass-server-bg-director', 'seabass-server-rabbitmq',
    'seabass-server-gateway', 'seabass-server-text-router', 'yq', 'python',
    'pghero', 'filebrowser',
}
images = []
def walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'image' and isinstance(value, str):
                images.append(value)
            walk(value)
    elif isinstance(obj, list):
        for value in obj:
            walk(value)
walk(objects)
walk(world)
assert {img.rsplit('/', 1)[-1].split(':')[0] for img in images} == allowed
namespaces = set()
for obj in objects:
    if obj['kind'].endswith('WebhookConfiguration'):
        for hook in obj['webhooks']:
            service = hook['clientConfig']['service']
            namespaces.add(service['namespace'])
            assert ('Service', service['name']) in indexed
        certificate = obj['metadata']['annotations']['cert-manager.io/inject-ca-from']
        ns, name = certificate.split('/')
        namespaces.add(ns)
        assert ('Certificate', name) in indexed
    if obj['kind'] == 'ClusterRoleBinding':
        namespaces.update(s['namespace'] for s in obj.get('subjects', []) if s['kind'] == 'ServiceAccount')
assert len(namespaces) == 1
namespace = next(iter(namespaces))
if len(sys.argv) > 2:
    assert namespace == sys.argv[2], f'Expected namespace {sys.argv[2]}, got {namespace}'
cluster_kinds = {'CustomResourceDefinition', 'ClusterRole', 'ClusterRoleBinding',
                 'MutatingWebhookConfiguration', 'ValidatingWebhookConfiguration'}
for obj in objects:
    if obj['kind'] in cluster_kinds:
        assert 'namespace' not in obj['metadata'], f"{obj['kind']} must be cluster scoped"
    else:
        assert obj['metadata'].get('namespace') == namespace, obj['metadata']
    if obj['kind'] in ('RoleBinding', 'ClusterRoleBinding'):
        for subject in obj.get('subjects', []):
            if subject['kind'] == 'ServiceAccount':
                assert subject.get('namespace') == namespace, subject
for obj in objects:
    if obj['kind'] == 'ClusterRole':
        for rule in obj['rules']:
            assert set(rule['verbs']) <= {'get', 'list', 'watch'}, rule
            assert 'pods/exec' not in rule['resources'], rule
    if obj['kind'] == 'ClusterRoleBinding':
        name = obj['roleRef']['name']
        assert ('Role', name) in indexed
        assert any(binding['kind'] == 'RoleBinding' and
                   binding['roleRef'] == dict(obj['roleRef'], kind='Role') and
                   binding['subjects'] == obj['subjects'] for binding in objects)
    if obj['kind'] in ('Deployment', 'Job'):
        pod = obj['spec']['template']['spec']
        assert pod['securityContext']['runAsNonRoot'] is True
        assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
        for container in pod.get('initContainers', []) + pod['containers']:
            assert container['securityContext']['allowPrivilegeEscalation'] is False
            assert container['securityContext']['capabilities']['drop'] == ['ALL']
    if obj['kind'] == 'Certificate' and 'dnsNames' in obj['spec']:
        assert all(f'.{namespace}.svc' in dns for dns in obj['spec']['dnsNames'])
print(f'Validated {len(objects)} resources, {len(images)} image references, namespace {namespace}')
