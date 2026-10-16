"""Consistent offline snapshots with checksums and a testable restore procedure."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from .database import writer_lock
from .storage import as_json, digest, intact, save


def create_backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError('Backup must be outside the synchronization data directory')
    if destination.exists() or not (source / 'sync.sqlite3').is_file():
        raise ValueError('Backup destination exists or database missing')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with writer_lock(source):
        temporary = Path(tempfile.mkdtemp(prefix='.momas-backup-', dir=destination.parent))
        try:
            for path in source.rglob('*'):
                if path.is_symlink():
                    raise ValueError('Symlinks not allowed in backup')
                if not path.is_file() or path.name in ('.sync.lock', 'sync.sqlite3', 'sync.sqlite3-wal', 'sync.sqlite3-shm'):
                    continue
                save(temporary / path.relative_to(source), path.read_bytes())
            with closing(sqlite3.connect((source / 'sync.sqlite3').as_uri() + '?mode=ro', uri=True)) as live:
                with closing(sqlite3.connect(temporary / 'sync.sqlite3')) as snapshot:
                    live.backup(snapshot)
                    snapshot.execute('PRAGMA journal_mode=DELETE')
            checksums = {str(path.relative_to(temporary)): digest(path.read_bytes())
                         for path in temporary.rglob('*') if path.is_file()}
            save(temporary / 'manifest.json', as_json({'version': 1, 'sha256': checksums}))
            verify_backup(temporary)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def verify_backup(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('version') != 1 or not isinstance(manifest.get('sha256'), dict):
        raise ValueError('Unsupported backup manifest')
    listed = manifest['sha256']
    actual = {str(path.relative_to(directory)) for path in directory.rglob('*')
              if path.is_file() and path.name != 'manifest.json'}
    if actual != set(listed):
        raise ValueError('Backup file set mismatch')
    for name, checksum in listed.items():
        path = directory / name
        if path.is_symlink() or directory not in path.resolve().parents or not intact(path, checksum):
            raise ValueError('Backup checksum mismatch')
    database = directory / 'sync.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as snapshot:
        snapshot.row_factory = sqlite3.Row
        if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity failure')
        for row in snapshot.execute('SELECT * FROM rides'):
            references = [('metadata_path', 'metadata_sha')]
            if row['raw_path']:
                references.append(('raw_path', 'raw_sha'))
            if row['status'] == 'ready':
                references.append(('export_path', 'export_sha'))
            for field, sha in references:
                relative = row[field]
                if not relative or relative not in listed or listed[relative] != row[sha]:
                    raise ValueError('Backup database references do not match files')

        if snapshot.execute('PRAGMA user_version').fetchone()[0] >= 2:
            for upload in snapshot.execute('SELECT * FROM uploads'):
                for path_field, sha_field in (('request_path','request_sha'), ('response_path','response_sha')):
                    relative = upload[path_field]
                    if relative and (relative not in listed or listed[relative] != upload[sha_field]):
                        raise ValueError('Backup upload receipt checksum mismatch')
                if upload['status'] in ('uploaded','verified') and not upload['response_path']:
                    raise ValueError('Backup upload receipt missing')
