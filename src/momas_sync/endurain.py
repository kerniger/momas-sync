"""Endurain v0.19.3 uploads: private receipts and no blind POST retries."""
import http.client
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import json
import math
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .storage import as_json, digest, intact, ride_key, save
from .timestamps import parse_gpx


class UploadError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class UploadRejected(UploadError):
    pass


class UploadUnknown(UploadError):
    """Request may already have created an activity; reconciliation required."""


class EndurainClient:
    def __init__(self, url, api_key, timeout=120):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Endurain URL must be a base origin')
        if not api_key or any(char in api_key for char in '\r\n'):
            raise ValueError('Endurain API key required')
        self.url = f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'
        self.api_key, self.timeout = api_key, timeout

    def upload(self, body, file_type='gpx'):
        if file_type == 'gpx':
            parse_gpx(body)
        elif file_type == 'tcx':
            text = body.decode('utf-8')
            if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper() or '\x00' in text:
                raise ValueError('Unsafe TCX XML')
            if ET.fromstring(body).tag != '{http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2}TrainingCenterDatabase':
                raise ValueError('Invalid TCX root')
        else:
            raise ValueError('Unsupported Endurain format')
        boundary = 'momas-' + uuid.uuid4().hex
        multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="momas.{file_type}"\r\n'
                     f'Content-Type: application/{file_type}+xml\r\n\r\n').encode() + body + f'\r\n--{boundary}--\r\n'.encode()
        request = urllib.request.Request(self.url + '/api/v1/activities/create/upload',
            data=multipart, method='POST', headers={
                'X-API-Key': self.api_key,
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Accept': 'application/json', 'User-Agent': 'momas-sync/0.4.0'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024 or response.status != 201:
                    raise UploadUnknown('unexpected_upload_response')
                result = json.loads(raw)
                if (not isinstance(result, list) or len(result) != 1
                        or not isinstance(result[0], dict)
                        or isinstance(result[0].get('id'), bool)
                        or not isinstance(result[0].get('id'), int) or result[0]['id'] < 1):
                    raise UploadUnknown('invalid_upload_receipt')
                return result
        except urllib.error.HTTPError as exc:
            # A 5xx/timeout may occur after the activity has been committed.
            if 400 <= exc.code < 500 and exc.code not in (408, 409):
                raise UploadRejected(f'endurain_http_{exc.code}') from None
            raise UploadUnknown(f'endurain_http_{exc.code}') from None
        except (urllib.error.URLError, OSError, TimeoutError, EOFError, http.client.HTTPException):
            raise UploadUnknown('endurain_transport_error') from None
        except (ValueError, UnicodeError):
            raise UploadUnknown('invalid_upload_receipt') from None


def gpx_statistics(body):
    root = parse_gpx(body)
    times, power, cadence = [], [], []
    for point in root.iter():
        if point.tag.split('}')[-1] != 'trkpt':
            continue
        for node in point:
            if node.tag.split('}')[-1] == 'time' and node.text:
                times.append(node.text)
            if node.tag.split('}')[-1] != 'extensions':
                continue
            for extension in node.iter():
                tag = extension.tag.split('}')[-1].lower()
                if tag not in ('power', 'powerinwatts', 'cad', 'cadence'):
                    continue
                try:
                    value = float(extension.text)
                    if not math.isfinite(value):
                        continue
                except (ValueError, TypeError):
                    continue
                (power if tag in ('power', 'powerinwatts') else cadence).append(value)
    return {'trackpoints': len(times), 'first_time': times[0] if times else None,
            'last_time': times[-1] if times else None,
            'power_samples': len(power), 'power_zero_samples': sum(value == 0 for value in power),
            'power_nonzero_average': sum(value for value in power if value != 0) / len([value for value in power if value != 0]) if any(power) else None,
            'power_average': sum(power) / len(power) if power else None,
            'power_max': max(power) if power else None,
            'cadence_samples': len(cadence), 'cadence_average': sum(cadence) / len(cadence) if cadence else None,
            'cadence_max': max(cadence) if cadence else None}


def test_upload(settings, store, client):
    # Always choose the same oldest ready activity. Re-running this test never
    # silently progresses to uploading a second ride.
    previous_test = store.connection.execute('SELECT * FROM uploads WHERE target=? ORDER BY created_at LIMIT 1', (client.url,)).fetchone()
    if previous_test:
        if previous_test['status'] in ('uploaded', 'verified'):
            return {'status': 'already_uploaded', 'uploaded': 0, 'skipped': 1}
        raise UploadError('upload_requires_reconciliation')
    rows = [row for row in store.rides() if row['status'] == 'ready']
    rows.sort(key=lambda row: json.loads(row['check_json']).get('api_start_utc') or '')
    if not rows:
        raise UploadError('no_ready_ride')
    row = rows[0]
    existing = store.upload_record(client.url, row['ride_id'])
    if existing:
        if existing['status'] in ('uploaded', 'verified'):
            return {'status': 'already_uploaded', 'uploaded': 0, 'skipped': 1}
        raise UploadError('upload_requires_reconciliation')
    path = settings.data_dir / row['export_path']
    if not intact(path, row['export_sha']):
        raise UploadError('export_checksum_mismatch')
    body = path.read_bytes()
    private = Path('deliveries') / digest(client.url.encode()) / ride_key(row['ride_id'])
    request_path = private / f'{row["export_sha"]}.gpx'
    save(settings.data_dir / request_path, body)
    # Persist intent before sending. A crash anywhere afterwards remains blocked
    # until the remote state is reconciled; the server offers no idempotency key.
    store.begin_upload(client.url, row['ride_id'], str(request_path), row['export_sha'])
    try:
        response = client.upload(body)
        response_path = private / 'response.json'
        response_body = as_json(response)
        save(settings.data_dir / response_path, response_body)
        store.finish_upload(client.url, row['ride_id'], 'uploaded', response[0]['id'],
                            str(response_path), digest(response_body))
        activity = response[0]
        save(settings.data_dir / private / 'comparison.json', as_json({
            'gpx': gpx_statistics(body), 'bafang': {key: json.loads(row['metadata_json']).get(key)
                for key in ('startTime', 'endTime', 'trip', 'duration', 'avgPower', 'maxPower', 'avgCadence')},
            'endurain': {key: activity.get(key) for key in ('id', 'start_time', 'end_time', 'start_time_tz_applied',
                'timezone', 'distance', 'total_elapsed_time', 'total_timer_time', 'average_power', 'max_power',
                'average_cad', 'max_cad', 'activity_type', 'visibility')},
            'stream_completeness_verified': False,
        }))
        return {'status': 'uploaded', 'uploaded': 1, 'skipped': 0}
    except UploadRejected as exc:
        store.finish_upload(client.url, row['ride_id'], 'rejected', error_code=exc.code)
        raise
    except UploadUnknown as exc:
        store.finish_upload(client.url, row['ride_id'], 'unknown', error_code=exc.code)
        raise


def validate_tcx_response(ride, gpx, activity):
    expected = gpx_statistics(gpx)
    checks = {}
    for metric, source in (('average_power','power_average'),('max_power','power_max'),
                           ('average_cad','cadence_average'),('max_cad','cadence_max')):
        value = expected[source]
        remote = activity.get(metric)
        checks[metric] = value is None or (isinstance(remote,(int,float)) and abs(remote-value) <= 1)
    checks['distance'] = isinstance(activity.get('distance'),(int,float)) and abs(activity['distance'] - float(ride['trip'])*1000) <= 1
    for name, field in (('first_time','start_time_tz_applied'),('last_time','end_time_tz_applied')):
        try:
            original = datetime.fromisoformat(expected[name].replace('Z','+00:00'))
            local = original.astimezone(ZoneInfo(activity['timezone'])).replace(tzinfo=None)
            remote = datetime.fromisoformat(activity[field])
            if remote.tzinfo is not None:
                remote = remote.astimezone(ZoneInfo(activity['timezone'])).replace(tzinfo=None)
            checks[field] = abs((local-remote).total_seconds()) < 1
        except (ValueError,KeyError,TypeError):
            checks[field] = False
    try:
        duration = (datetime.fromisoformat(expected['last_time'].replace('Z','+00:00')) - datetime.fromisoformat(expected['first_time'].replace('Z','+00:00'))).total_seconds()
        checks['duration'] = abs(activity['total_elapsed_time'] - duration) <= 1
    except (KeyError,TypeError,ValueError):
        checks['duration'] = False
    return {'summary_verified':all(checks.values()), 'checks':checks,
            'stream_sample_readback_verified':False, 'gpx':expected}


def upload_ready(settings, store, client, stop=None, heartbeat=lambda: None):
    """Upload each new ready ride as TCX. Uncertain requests are never retried."""
    from .tcx import to_tcx
    counts = {'uploaded':0,'upload_skipped':0,'upload_blocked':0,'upload_errors':0,'legacy_gpx':0}
    for row in store.rides():
        if stop and stop.is_set():
            from .retry import Cancelled
            raise Cancelled()
        if row['status'] != 'ready':
            continue
        heartbeat()
        previous = store.upload_record(client.url,row['ride_id'])
        if previous:
            if previous['status'] in ('uploaded','verified'):
                counts['upload_skipped'] += 1
                if previous['error_code'] == 'summary_mismatch':
                    counts['upload_errors'] += 1
                if previous['request_path'].endswith('.gpx'):
                    counts['legacy_gpx'] += 1
                continue
            counts['upload_blocked'] += 1
            continue
        gpx_path = settings.data_dir/row['export_path']
        if not intact(gpx_path,row['export_sha']):
            counts['upload_errors'] += 1
            continue
        ride = json.loads(row['metadata_json'])
        gpx = gpx_path.read_bytes()
        try:
            body = to_tcx(ride,gpx)
        except (ValueError,KeyError,OverflowError):
            counts['upload_errors'] += 1
            continue
        checksum = digest(body)
        private = Path('deliveries')/digest(client.url.encode())/ride_key(row['ride_id'])
        request_path = private/f'{checksum}.tcx'
        save(settings.data_dir/request_path,body)
        save(settings.data_dir/Path(row['export_path']).with_suffix('.tcx'),body)
        store.begin_upload(client.url,row['ride_id'],str(request_path),checksum)
        try:
            response = client.upload(body,file_type='tcx')
            response_path = private/'response.json'
            response_body = as_json(response)
            save(settings.data_dir/response_path,response_body)
            # Record successful receipt before any optional validation work.
            store.finish_upload(client.url,row['ride_id'],'uploaded',response[0]['id'],str(response_path),digest(response_body))
            check = validate_tcx_response(ride,gpx,response[0])
            save(settings.data_dir/private/'comparison.json',as_json(check))
            if check['summary_verified']:
                store.finish_upload(client.url,row['ride_id'],'verified',response[0]['id'],str(response_path),digest(response_body))
            else:
                store.finish_upload(client.url,row['ride_id'],'uploaded',response[0]['id'],str(response_path),digest(response_body),error_code='summary_mismatch')
                counts['upload_errors'] += 1
            counts['uploaded'] += 1
        except UploadError as exc:
            store.finish_upload(client.url,row['ride_id'],'rejected' if isinstance(exc,UploadRejected) else 'unknown',error_code=exc.code)
            counts['upload_errors'] += 1
        heartbeat()
    return counts
