"""SQLite status store, with a versioned schema and one writer per data directory."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import time


class AlreadyRunning(Exception):
    pass


@contextmanager
def writer_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning() from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.directory / 'sync.sqlite3', timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA busy_timeout=10000')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2):
            self.connection.close()
            raise ValueError('Unsupported database schema version')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        if version == 0:
            with self.connection:
                self.connection.executescript('''
                    CREATE TABLE rides (
                        ride_id TEXT PRIMARY KEY,
                        metadata_json TEXT NOT NULL,
                        metadata_sha TEXT NOT NULL,
                        metadata_path TEXT NOT NULL,
                        first_seen REAL NOT NULL,
                        last_seen REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','ready','review','error')),
                        raw_path TEXT,
                        raw_sha TEXT,
                        raw_metadata_sha TEXT,
                        export_path TEXT,
                        export_sha TEXT,
                        correction_seconds INTEGER,
                        check_json TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT
                    );
                    CREATE TABLE state (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        status TEXT NOT NULL,
                        heartbeat REAL NOT NULL,
                        last_attempt REAL,
                        last_success REAL,
                        cycles INTEGER NOT NULL DEFAULT 0,
                        counts_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT
                    );
                    INSERT INTO state(id,status,heartbeat) VALUES(1,'starting',0);
                    PRAGMA user_version=1;
                ''')

        if version in (0, 1):
            with self.connection:
                self.connection.execute("""CREATE TABLE uploads (
                    target TEXT NOT NULL,
                    ride_id TEXT NOT NULL REFERENCES rides(ride_id),
                    status TEXT NOT NULL CHECK(status IN ('inflight','uploaded','verified','unknown','rejected')),
                    request_path TEXT NOT NULL,
                    request_sha TEXT NOT NULL,
                    response_path TEXT,
                    response_sha TEXT,
                    activity_id INTEGER,
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(target,ride_id)
                )""")
                self.connection.execute('PRAGMA user_version=2')

    def close(self):
        self.connection.close()

    def heartbeat(self):
        with self.connection:
            self.connection.execute('UPDATE state SET heartbeat=? WHERE id=1', (time.time(),))

    def begin_cycle(self):
        with self.connection:
            self.connection.execute("UPDATE state SET status='syncing',last_attempt=?,heartbeat=?,error_code=NULL WHERE id=1", (time.time(), time.time()))

    def finish_cycle(self, status, counts, error_code=None):
        now = time.time()
        with self.connection:
            self.connection.execute('''UPDATE state SET status=?, heartbeat=?, cycles=cycles+1,
                counts_json=?, error_code=?, last_success=CASE WHEN ?='ok' THEN ? ELSE last_success END WHERE id=1''',
                (status, now, json.dumps(counts, sort_keys=True), error_code, status, now))

    def stop(self):
        with self.connection:
            self.connection.execute("UPDATE state SET status='stopped',heartbeat=? WHERE id=1", (time.time(),))

    def state(self):
        return dict(self.connection.execute('SELECT * FROM state WHERE id=1').fetchone())

    def upsert(self, ride, metadata_sha, metadata_path):
        ride_id = str(ride['id'])
        previous = self.get(ride_id)
        with self.connection:
            self.connection.execute('''INSERT INTO rides(ride_id,metadata_json,metadata_sha,metadata_path,first_seen,last_seen)
                VALUES(?,?,?,?,?,?) ON CONFLICT(ride_id) DO UPDATE SET
                metadata_json=excluded.metadata_json,metadata_sha=excluded.metadata_sha,
                metadata_path=excluded.metadata_path,last_seen=excluded.last_seen,
                status=CASE WHEN rides.metadata_sha<>excluded.metadata_sha THEN 'pending' ELSE rides.status END,
                export_path=CASE WHEN rides.metadata_sha<>excluded.metadata_sha THEN NULL ELSE rides.export_path END,
                export_sha=CASE WHEN rides.metadata_sha<>excluded.metadata_sha THEN NULL ELSE rides.export_sha END''',
                (ride_id, json.dumps(ride, sort_keys=True), metadata_sha, metadata_path, time.time(), time.time()))
        return previous is None

    def get(self, ride_id):
        row = self.connection.execute('SELECT * FROM rides WHERE ride_id=?', (str(ride_id),)).fetchone()
        return dict(row) if row else None

    def rides(self):
        return [dict(row) for row in self.connection.execute('SELECT * FROM rides ORDER BY first_seen,ride_id')]

    def attempt(self, ride_id):
        with self.connection:
            self.connection.execute('UPDATE rides SET attempts=attempts+1 WHERE ride_id=?', (ride_id,))

    def downloaded(self, ride_id, path, checksum, metadata_sha):
        with self.connection:
            self.connection.execute("UPDATE rides SET raw_path=?,raw_sha=?,raw_metadata_sha=?,status='pending',error_code=NULL WHERE ride_id=?", (path, checksum, metadata_sha, ride_id))

    def ready(self, ride_id, path, checksum, correction, check):
        with self.connection:
            self.connection.execute("UPDATE rides SET status='ready',export_path=?,export_sha=?,correction_seconds=?,check_json=?,error_code=NULL WHERE ride_id=?", (path, checksum, correction, json.dumps(check, sort_keys=True), ride_id))

    def failed(self, ride_id, code, review=False, check=None):
        with self.connection:
            self.connection.execute('''UPDATE rides SET status=?,error_code=?,export_path=NULL,export_sha=NULL,
                check_json=?,correction_seconds=NULL WHERE ride_id=?''',
                ('review' if review else 'error', code, json.dumps(check, sort_keys=True) if check else None, ride_id))

    def upload_record(self, target, ride_id):
        row = self.connection.execute('SELECT * FROM uploads WHERE target=? AND ride_id=?', (target, ride_id)).fetchone()
        return dict(row) if row else None

    def begin_upload(self, target, ride_id, request_path, request_sha):
        with self.connection:
            self.connection.execute("""INSERT INTO uploads(target,ride_id,status,request_path,request_sha,created_at,updated_at)
                VALUES(?,?,'inflight',?,?,?,?)""", (target, ride_id, request_path, request_sha, time.time(), time.time()))

    def finish_upload(self, target, ride_id, status, activity_id=None, response_path=None, response_sha=None, error_code=None):
        with self.connection:
            self.connection.execute("""UPDATE uploads SET status=?,activity_id=?,response_path=?,response_sha=?,error_code=?,updated_at=?
                WHERE target=? AND ride_id=?""", (status, activity_id, response_path, response_sha, error_code, time.time(), target, ride_id))
