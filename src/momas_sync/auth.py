"""Momas Red password login and private, persistent sessions.

Protocol observed in Android 1.0.1: form POST login/10001, account and
lowercase MD5(UTF-8 password), AppResponse.Login with expiredTimeStamp.
No account registration, profile updates or CAPTCHA bypass is performed.
"""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import time
import urllib.parse
import urllib.request

from .client import ApiError, AuthenticationError, BafangClient, request_bytes
from .storage import as_json, save

HOST = "https://msbg.bafang-e.com.cn:8096"


def device_id(directory):
    configured = os.getenv('BAFANG_DEVICE_ID', '').strip()
    if configured:
        return configured
    path = Path(directory) / 'auth' / 'device_id'
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        value = secrets.token_hex(8)
        save(path, (value + '\n').encode())
    if len(value) != 16 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid persisted device ID')
    return value


def secret_value(name, *, password=False):
    filename = os.getenv(name + '_FILE')
    if filename:
        # Remove one editor-added line ending, preserving password spaces.
        value = Path(filename).read_text(encoding='utf-8').removesuffix('\n').removesuffix('\r')
    else:
        value = os.getenv(name, '')
    return value if password else value.strip()


def expiry_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ApiError('Login response has invalid expiry')
    # Epoch seconds or milliseconds; actual unit is verified during live login.
    return value / 1000 if value >= 100_000_000_000 else float(value)


def password_login(account, password, device_id, timeout=30):
    if not account or not password or not device_id:
        raise ValueError('Momas account, password and device ID are required')
    body = urllib.parse.urlencode({
        'account': account,
        'password': hashlib.md5(password.encode('utf-8'), usedforsecurity=False).hexdigest(),
    }).encode('ascii')
    request = urllib.request.Request(HOST + '/BaFangCyclingApi/login/10001', data=body,
        headers={'AppClient': 'com.momasbikes.ebike', 'userDeviceId': device_id,
                 'User-Agent': 'okhttp/5.0.0-alpha.2', 'Accept-Encoding': 'gzip',
                 'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        payload = json.loads(request_bytes(request, timeout))
    except (ValueError, UnicodeError):
        raise ApiError('Login response is not JSON') from None
    if not isinstance(payload, dict):
        raise ApiError('Unknown login response schema')
    code = payload.get('code')
    # 202 is explicitly handled as failed login by the Momas Android UI.
    if code in (202, 401, 403, '202', '401', '403'):
        raise AuthenticationError('Momas login rejected')
    if code not in (200, '200'):
        raise ApiError('Momas login reported an application error')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ApiError('Login response is missing session data')
    token = data.get('token')
    if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
        raise ApiError('Login response has invalid token')
    return token, expiry_seconds(data.get('expiredTimeStamp'))


class LoginSession:
    """A session per sync cycle, with at most one re-login after rejection.

    Rejected credentials or a second token rejection remain blocked across
    restarts until credentials change or the operator runs auth-reset.
    Transient failures are retried by the existing GET-operation backoff.
    """
    def __init__(self, directory, account, password, device_id, timeout=30, clock=time.time,
                 login=password_login, client_factory=BafangClient):
        if not account or not password or not device_id:
            raise ValueError('Momas account, password and device ID are required')
        self.path = Path(directory) / 'auth' / 'session.json'
        self.account, self.password, self.device_id = account, password, device_id
        self.timeout, self.clock, self.login, self.client_factory = timeout, clock, login, client_factory
        self.reauthenticated = False
        self.state = self._load()

    def _load(self):
        try:
            if self.path.stat().st_size > 65536:
                raise ValueError()
            state = json.loads(self.path.read_bytes())
            if not isinstance(state, dict) or state.get('version') != 1:
                raise ValueError()
            key = bytes.fromhex(state['binding_key'])
            if len(key) != 32:
                raise ValueError()
            binding = state.get('binding')
            if not isinstance(binding, str) or len(binding) != 64 or any(c not in '0123456789abcdef' for c in binding):
                raise ValueError()
            token, expires = state.get('token'), state.get('expires_at')
            if token is not None and (not isinstance(token, str) or not token or len(token) > 16384
                    or any(c.isspace() for c in token) or isinstance(expires, bool)
                    or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= 0):
                raise ValueError()
        except FileNotFoundError:
            state = {'version': 1, 'binding_key': secrets.token_hex(32)}
            key = bytes.fromhex(state['binding_key'])
        except (ValueError, KeyError, TypeError):
            # A damaged cache must not create an uncontrolled login loop.
            raise ApiError('Session cache invalid; run auth-reset') from None
        binding = hmac.new(key, as_json([self.account, self.password, self.device_id, HOST]), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(state.get('binding', '')), binding):
            state = {'version': 1, 'binding_key': key.hex(), 'binding': binding}
        return state

    def _save(self):
        save(self.path, as_json(self.state))  # atomic, mode 0600

    def _block(self):
        self.state.pop('token', None)
        self.state.pop('expires_at', None)
        self.state['blocked'] = True
        self._save()

    def _token(self):
        if self.state.get('blocked'):
            raise AuthenticationError('Momas login blocked; update secrets or run auth-reset')
        token, expires = self.state.get('token'), self.state.get('expires_at')
        if (isinstance(token, str) and token and isinstance(expires, (int, float))
                and not isinstance(expires, bool) and math.isfinite(expires) and expires > self.clock() + 60):
            return token
        # Discard an expired or rejected token even if the next request fails.
        self.state.pop('token', None)
        self.state.pop('expires_at', None)
        self._save()
        try:
            token, expires = self.login(self.account, self.password, self.device_id, self.timeout)
        except AuthenticationError:
            self._block()
            raise
        if expires <= self.clock() + 60:
            self._block()
            raise AuthenticationError('Momas returned an expired session; check system clock')
        self.state.update(token=token, expires_at=expires, blocked=False)
        self._save()
        return token

    def _call(self, method, *args):
        while True:
            client = self.client_factory(self._token(), self.device_id, timeout=self.timeout)
            try:
                return getattr(client, method)(*args)
            except AuthenticationError:
                self.state.pop('token', None)
                self.state.pop('expires_at', None)
                if self.reauthenticated:
                    self._block()
                    raise AuthenticationError('Momas rejected renewed session; login blocked') from None
                self.reauthenticated = True
                self._save()

    def list_page(self, page, size=20):
        return self._call('list_page', page, size)

    def download_gpx(self, ride_id):
        return self._call('download_gpx', ride_id)
