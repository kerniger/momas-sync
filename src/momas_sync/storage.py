"""Private, atomic file storage shared by the PoC and synchronization service."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def save(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    # Persist the rename before SQLite may record the file as complete.
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def as_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def ride_key(ride_id):
    return digest(str(ride_id).encode())


def intact(path, expected_hash):
    try:
        return bool(expected_hash) and digest(Path(path).read_bytes()) == expected_hash
    except FileNotFoundError:
        return False
