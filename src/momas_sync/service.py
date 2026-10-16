import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
from .database import AlreadyRunning, Store, writer_lock
from .health import healthy, read_state
from .retry import Cancelled, Retrier
from .settings import Settings
from .sync import Synchronizer


def log(event, **fields):
    # Call sites supply only static status codes and numeric counts.
    print(json.dumps({'event': event, **fields}, sort_keys=True), flush=True)


def serve(settings, once=False, stop=None, client_factory=None):
    stop = stop or threading.Event()
    with writer_lock(settings.data_dir):
        store = Store(settings.data_dir)
        worker = Synchronizer(settings, store, client_factory, stop, log)
        try:
            while not stop.is_set():
                summary = worker.cycle()
                if once:
                    return 0 if summary['status'] == 'ok' else 1
                try:
                    worker.retrier.wait(settings.interval)
                except Cancelled:
                    break
            return 0
        except Cancelled:
            return 0
        finally:
            if not once:
                store.stop()
                log('sync_stopped')
            store.close()


def main():
    parser = argparse.ArgumentParser(description='Momas read-only synchronization service')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('once', help='Run one synchronization cycle')
    sub.add_parser('serve', help='Poll continuously, default every 30 minutes')
    sub.add_parser('healthcheck', help='Read-only health check (exit 0/1)')
    sub.add_parser('endurain-test', help='Upload exactly one ready ride; never retry uncertain POSTs')
    sub.add_parser('status', help='Print status and counts without private ride metadata')
    sub.add_parser('login-test', help='Verify password login and read ride list without uploads')
    sub.add_parser('auth-reset', help='Clear cached session and login block; stops concurrent writers')
    backup = sub.add_parser('backup', help='Consistent backup; service must be stopped')
    backup.add_argument('destination', type=Path)
    verify = sub.add_parser('verify-backup', help='Verify backup hashes and SQLite integrity')
    verify.add_argument('directory', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    stop = threading.Event()
    if args.command in ('serve', 'once'):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    try:
        settings = Settings.from_env()
        if args.command in ('login-test', 'auth-reset'):
            from .auth import LoginSession
            from .client import ApiError, AuthenticationError, extract_rides
            with writer_lock(settings.data_dir):
                if args.command == 'auth-reset':
                    (settings.data_dir / 'auth' / 'session.json').unlink(missing_ok=True)
                    log('auth_reset')
                    return 0
                try:
                    client = settings.client()
                    if not isinstance(client, LoginSession):
                        raise ValueError('Password login configuration is required')
                    payload = Retrier(settings, stop).call(lambda: client.list_page(1, settings.page_size))
                    log('login_verified', listed_rides=len(extract_rides(payload)))
                    return 0
                except AuthenticationError:
                    log('login_failed', error_code='authentication_rejected')
                    return 1
                except ApiError:
                    log('login_failed', error_code='network_or_api_error')
                    return 1
        if args.command == 'endurain-test':
            from .endurain import EndurainClient, UploadError, test_upload
            key_file = os.getenv('ENDURAIN_API_KEY_FILE')
            key = Path(key_file).read_text().strip() if key_file else os.getenv('ENDURAIN_API_KEY', '').strip()
            client = EndurainClient(os.getenv('ENDURAIN_URL', ''), key)
            with writer_lock(settings.data_dir):
                store = Store(settings.data_dir)
                try:
                    summary = test_upload(settings, store, client)
                    log('endurain_test', **summary)
                    return 0
                except UploadError as exc:
                    log('endurain_test_failed', error_code=exc.code)
                    return 1
                finally:
                    store.close()
        if args.command == 'healthcheck':
            return 0 if healthy(settings) else 1
        if args.command == 'status':
            state = read_state(settings.data_dir)
            log('status', status=state['status'], last_attempt=state['last_attempt'],
                last_success=state['last_success'], cycles=state['cycles'],
                counts=json.loads(state['counts_json']), error_code=state['error_code'])
            return 0
        if args.command in ('backup', 'verify-backup'):
            from .backup import create_backup, verify_backup
            if args.command == 'backup':
                create_backup(settings.data_dir, args.destination)
            else:
                verify_backup(args.directory)
            log('backup_verified' if args.command == 'verify-backup' else 'backup_created')
            return 0
        return serve(settings, once=args.command == 'once', stop=stop)
    except AlreadyRunning:
        log('failed', error_code='writer_already_running_stop_service_before_backup')
        return 1
    except (OSError, ValueError, sqlite3.Error, KeyError, OverflowError):
        log('failed', error_code='configuration_or_storage_error')
        return 1
    except Exception:
        # No traceback: external exception messages can include a token URL or GPS.
        log('failed', error_code='unexpected_error')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
