"""Verify publication guardrails against accidental private-data exports."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release_check', Path(__file__).parents[1] / 'scripts/check_release.py')
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.override = patch.object(release_check, 'ROOT', self.root)
        self.override.start()
        self.addCleanup(self.override.stop)
        self.git('init', '--quiet')
        for name in ('README.md', 'LICENSE', 'SECURITY.md', '.gitignore', '.dockerignore',
                     '.env.example', 'Dockerfile', 'compose.yaml', 'pyproject.toml'):
            (self.root / name).write_text('synthetic source\n')
        self.git('add', '.')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def add(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git('add', '--force', name)
        return path

    def test_public_source_and_synthetic_fixture_allowed(self):
        self.add('tests/fixtures/ride.gpx', '<gpx creator="synthetic"/>')
        files = release_check.checked_files()
        self.assertIn('tests/fixtures/ride.gpx', {name for name, _ in files})

    def test_private_paths_rejected_even_when_force_added(self):
        for name in ('secrets/password', 'data/original.json', 'private/report.md',
                     '.env', '.env.production', 'ride.gpx', 'backup.sqlite3-wal'):
            with self.subTest(name=name):
                self.add(name, 'synthetic private data')
                with self.assertRaises(ValueError):
                    release_check.checked_files()
                self.git('rm', '--cached', '--force', name)

    def test_secret_patterns_rejected_without_printing_values(self):
        samples = ['.'.join(('192', '168', '22', '44')), 'endurain_' + 'A' * 32,
                   'ghp_' + 'B' * 36, 'token=' + 'c' * 128]
        for value in samples:
            self.add('notes.md', value)
            with self.assertRaises(ValueError) as caught:
                release_check.checked_files()
            self.assertNotIn(value, str(caught.exception))

    def test_staged_content_cannot_differ_from_export(self):
        path = self.add('notes.md', 'endurain_' + 'D' * 32)
        path.write_text('clean working copy')
        with self.assertRaises(ValueError):
            release_check.checked_files()

    def test_symlink_rejected(self):
        (self.root / 'linked.md').symlink_to(self.root / 'README.md')
        self.git('add', 'linked.md')
        with self.assertRaises(ValueError):
            release_check.checked_files()


if __name__ == '__main__':
    unittest.main()
