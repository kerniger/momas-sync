#!/usr/bin/env python3
"""Check Git-listed working files and optionally create a private-data-free ZIP.

No external dependencies. Errors print file names, never matching contents.
This is a guardrail, not a general-purpose secret detector.
"""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PARTS = {'data', 'secrets', 'private', '.git', '__pycache__', '.venv',
                 'venv', 'build', 'dist', '.pytest_cache'}
BLOCKED_SUFFIXES = {'.pyc', '.pyo', '.sqlite3', '.db', '.pem', '.key', '.p12', '.apk', '.xapk', '.log'}
# Public API hosts are permitted. Deployment-specific private network IPs are not.
PATTERNS = [
    re.compile(rb'\b192\.168\.\d{1,3}\.\d{1,3}\b'),
    re.compile(rb'\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'),
    re.compile(rb'\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b'),
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    re.compile(rb'\bendurain_[A-Za-z0-9_-]{24,}\b'),
    re.compile(rb'(?i)\btoken=[0-9a-f]{80,}\b'),
    re.compile(rb'/home/(?:ubuntu|[a-z][a-z0-9_-]*)/'),
]


def checked_files():
    result = subprocess.run(['git', 'ls-files', '-z', '--cached'], cwd=ROOT,
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    names = sorted(set(result.stdout.decode('utf-8').rstrip('\0').split('\0')) - {''})
    if not names:
        raise ValueError('No Git-listed files; initialize and stage the public source first')
    errors, files = [], []
    for name in names:
        relative = PurePosixPath(name)
        path = ROOT / name
        forbidden = (relative.is_absolute() or '..' in relative.parts
                     or bool(set(relative.parts) & BLOCKED_PARTS)
                     or (relative.name.startswith('.env') and relative.name != '.env.example')
                     or relative.suffix.lower() in BLOCKED_SUFFIXES
                     or '.sqlite3-' in relative.name or '.db-' in relative.name
                     or (relative.suffix.lower() in ('.gpx', '.tcx', '.fit')
                         and relative.parts[:2] != ('tests', 'fixtures')))
        if forbidden or path.is_symlink() or not path.is_file():
            errors.append(f'Private, missing or unsupported path: {name}')
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            errors.append(f'Unexpected large source file: {name}')
            continue
        body = path.read_bytes()
        staged = subprocess.run(['git', 'show', ':' + name], cwd=ROOT, check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        if staged != body:
            errors.append(f'Working file differs from Git index; review and stage: {name}')
        if any(pattern.search(body) for pattern in PATTERNS):
            errors.append(f'Potential secret or private deployment detail: {name}')
        files.append((name, body))
    required = {'README.md', 'LICENSE', 'SECURITY.md', '.gitignore', '.dockerignore',
                '.env.example', 'Dockerfile', 'compose.yaml', 'pyproject.toml'}
    if required - set(names):
        errors.append('Required publication files missing')
    if errors:
        raise ValueError('\n'.join(errors))
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, help='Create an export of checked working files')
    args = parser.parse_args()
    try:
        files = checked_files()
        if args.zip:
            args.zip.parent.mkdir(parents=True, exist_ok=True)
            # Refuse overwrite of an existing file, including source files.
            with zipfile.ZipFile(args.zip, mode='x', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, body in files:
                    archive.writestr('momas-sync/' + name, body)
        print(f'Release check passed: {len(files)} public source files')
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError):
        # Filenames from our own validation are safe; external command messages aren't.
        print('Release check failed. Check tracked paths and private content.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
