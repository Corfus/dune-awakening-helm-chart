"""Run the GitHub packaging commands locally without publishing a release."""

import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / '.github/workflows/release-chart.yml').read_text(),
                     Loader=yaml.BaseLoader)
PACKAGE = next(step for step in WORKFLOW['jobs']['release-chart']['steps']
               if step.get('id') == 'package')


@unittest.skipUnless(shutil.which('helm'), 'Helm is required for release packaging checks')
class GithubReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chart = self.root / 'chart'
        shutil.copytree(ROOT, self.chart, ignore=shutil.ignore_patterns(
            '.git', 'dist', '.idea', '.agents', '.codex', '.aws', '__pycache__',
            'values.private*', '.env*', '*.key', '*.pem', 'secret.yaml'))
        self.version = yaml.safe_load((self.chart / 'Chart.yaml').read_text())['version']
        self.output = self.root / 'outputs'

    def package(self, tag):
        return subprocess.run(
            ['bash', '-e', '-o', 'pipefail', '-c', PACKAGE['run']],
            cwd=self.chart, text=True, capture_output=True,
            env={**os.environ, 'RELEASE_TAG': tag, 'GITHUB_OUTPUT': str(self.output)})

    def test_matching_tags_package_an_installable_chart_without_workflows(self):
        for tag in (self.version, f'v{self.version}'):
            with self.subTest(tag=tag):
                result = self.package(tag)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                archive = f'dist/dune-awakening-{self.version}.tgz'
                self.assertIn(f'archive={archive}', self.output.read_text().splitlines())
                with tarfile.open(self.chart / archive) as bundle:
                    names = bundle.getnames()
                    self.assertIn('dune-awakening/Chart.yaml', names)
                    self.assertTrue(any(name.startswith('dune-awakening/templates/')
                                        for name in names))
                    self.assertFalse(any('/.github/' in name for name in names))

    def test_mismatched_tag_fails_before_creating_an_archive(self):
        result = self.package('v999.0.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must match Chart.yaml version', result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.chart / 'dist').exists())


if __name__ == '__main__':
    unittest.main()
