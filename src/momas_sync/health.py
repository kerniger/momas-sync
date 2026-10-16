"""Read-only health assessment; never opens a missing database for writing."""
from contextlib import closing
import sqlite3
import time


def read_state(directory):
    path = (directory / 'sync.sqlite3').resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT * FROM state WHERE id=1').fetchone()
        if row is None:
            raise ValueError('No state')
        return dict(row)


def healthy(settings, now=None):
    try:
        state = read_state(settings.data_dir)
        now = time.time() if now is None else now
        # A heartbeat alone does not establish successful synchronization.
        heartbeat_age = now - state['heartbeat']
        success_age = now - state['last_success'] if state['last_success'] else float('inf')
        return (state['status'] in ('ok', 'syncing')
                and 0 <= heartbeat_age <= max(120, settings.timeout + 30)
                and 0 <= success_age <= settings.health_stale)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False
