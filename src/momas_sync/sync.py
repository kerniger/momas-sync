import json
import threading
from pathlib import Path
from .client import ApiError, AuthenticationError, TransientError, extract_rides
from .prepare import PREPARATION_VERSION, TimeReview, prepare_gpx
from .retry import Retrier
from .storage import as_json, digest, intact, ride_key, save


def error_code(error):
    if isinstance(error, AuthenticationError):
        return 'authentication_rejected'
    if isinstance(error, TransientError):
        return 'network_or_server_error'
    return 'api_response_error'


class Synchronizer:
    def __init__(self, settings, store, client_factory=None, stop=None, log=lambda *_args, **_kwargs: None):
        self.settings, self.store = settings, store
        self.directory = settings.data_dir
        self.client_factory = client_factory or settings.client
        self.stop = stop or threading.Event()
        self.log = log
        self.retrier = Retrier(settings, self.stop, store.heartbeat, log)

    def _invalidate_export(self, ride_id):
        for suffix in ('gpx', 'json', 'tcx'):
            (self.directory / 'exports' / f'{ride_key(ride_id)}.{suffix}').unlink(missing_ok=True)

    def _process(self, client, row, counts):
        ride_id = row['ride_id']
        raw_valid = (row['raw_path'] and row['raw_metadata_sha'] == row['metadata_sha']
                     and intact(self.directory / row['raw_path'], row['raw_sha']))
        check = json.loads(row['check_json']) if row['check_json'] else {}
        if (raw_valid and row['status'] == 'ready'
                and check.get('assumed_app_timezone') == self.settings.app_timezone
                and check.get('preparation_version') == PREPARATION_VERSION
                and row['export_path'] and intact(self.directory / row['export_path'], row['export_sha'])
                and (self.directory / 'exports' / f'{ride_key(ride_id)}.json').is_file()):
            counts['skipped'] += 1
            return
        self._invalidate_export(ride_id)
        if not raw_valid:
            self.store.attempt(ride_id)
            body = self.retrier.call(lambda: client.download_gpx(ride_id))
            checksum = digest(body)
            raw_path = Path('originals/gpx') / ride_key(ride_id) / f'{checksum}.gpx'
            if not intact(self.directory / raw_path, checksum):
                save(self.directory / raw_path, body)
            self.store.downloaded(ride_id, str(raw_path), checksum, row['metadata_sha'])
            row = self.store.get(ride_id)
            counts['downloaded'] += 1
        else:
            body = (self.directory / row['raw_path']).read_bytes()
        try:
            prepared, check = prepare_gpx(json.loads(row['metadata_json']), body, self.settings.app_timezone)
        except TimeReview as exc:
            self.store.failed(ride_id, exc.code, review=True, check=exc.check)
            counts['review'] += 1
            return
        export = Path('exports') / f'{ride_key(ride_id)}.gpx'
        checksum = digest(prepared)
        save(self.directory / export, prepared)
        save(self.directory / export.with_suffix('.json'), as_json({
            'checks': check, 'original_sha256': row['raw_sha'],
            'metadata_sha256': row['metadata_sha'], 'export_sha256': checksum,
            'original_path': row['raw_path'], 'export_path': str(export),
        }))
        self.store.ready(ride_id, str(export), checksum, check['correction_seconds'], check)
        counts['prepared'] += 1

    def _finish(self, status, counts, code=None):
        self.store.finish_cycle(status, counts, code)
        summary = {'status': status, 'counts': counts, 'error_code': code,
                   'last_success': self.store.state()['last_success']}
        save(self.directory / 'status.json', as_json(summary))
        self.log('sync_finished', status=status, **counts)
        return summary

    def cycle(self):
        counts = {'pages': 0, 'seen': 0, 'new': 0, 'downloaded': 0,
                  'prepared': 0, 'skipped': 0, 'review': 0, 'errors': 0,
                  'size_ignored_pages': 0}
        self.store.begin_cycle()
        self.log('sync_started')
        try:
            client = self.client_factory()
            seen = set()
            complete = False
            for page in range(1, self.settings.max_pages + 1):
                payload = self.retrier.call(lambda: client.list_page(page, self.settings.page_size))
                records = extract_rides(payload)
                save(self.directory / 'pages' / f'page-{page}.json', as_json(payload))
                counts['pages'] += 1
                metadata = payload.get('data', {}) if isinstance(payload, dict) else {}
                if isinstance(metadata, dict) and metadata.get('size') not in (None, self.settings.page_size):
                    counts['size_ignored_pages'] += 1
                if not records:
                    complete = True
                    break
                ids = {str(ride['id']) for ride in records}
                if ids <= seen:
                    raise ApiError('Pagination repeated a page')
                for ride in records:
                    ride_id = str(ride['id'])
                    if ride_id in seen:
                        continue
                    seen.add(ride_id)
                    content = as_json(ride)
                    checksum = digest(content)
                    path = Path('originals/rides') / ride_key(ride_id) / f'{checksum}.json'
                    if not intact(self.directory / path, checksum):
                        save(self.directory / path, content)
                    previous = self.store.get(ride_id)
                    if self.store.upsert(ride, checksum, str(path)):
                        counts['new'] += 1
                    elif previous['metadata_sha'] != checksum:
                        self._invalidate_export(ride_id)
            counts['seen'] = len(seen)
            for row in self.store.rides():
                self.store.heartbeat()
                if self.stop.is_set():
                    from .retry import Cancelled
                    raise Cancelled()
                try:
                    self._process(client, row, counts)
                except AuthenticationError as exc:
                    self.store.failed(row['ride_id'], error_code(exc))
                    raise
                except ApiError as exc:
                    self.store.failed(row['ride_id'], error_code(exc))
                    counts['errors'] += 1
            if self.settings.endurain_enabled and complete:
                from .endurain import upload_ready
                counts.update(upload_ready(self.settings,self.store,self.settings.endurain_client(),self.stop,self.store.heartbeat))
                if counts['upload_errors'] or counts['upload_blocked']:
                    return self._finish('degraded', counts, 'endurain_needs_attention')
            if counts['errors'] or counts['review'] or not complete:
                code = 'pagination_limit' if not complete else 'rides_need_attention'
                return self._finish('degraded', counts, code)
            return self._finish('ok', counts)
        except AuthenticationError as exc:
            return self._finish('auth_error', counts, error_code(exc))
        except ApiError as exc:
            return self._finish('degraded', counts, error_code(exc))
        except (ValueError, KeyError, OverflowError):
            return self._finish('error', counts, 'configuration_or_data_error')
        except OSError:
            return self._finish('error', counts, 'secret_or_storage_error')
