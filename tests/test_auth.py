import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

from momas_sync.auth import LoginSession, device_id, password_login, secret_value
from momas_sync.client import ApiError, AuthenticationError, TransientError
from momas_sync.settings import Settings
from momas_sync.service import main


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1_800_000_000
        self.login = Mock(return_value=('synthetic-token', self.now + 3600))
        self.client = Mock()
        self.client.list_page.return_value = {'code': 200, 'data': {'records': []}}
        self.factory = Mock(return_value=self.client)

    def session(self, **kwargs):
        return LoginSession(self.root, kwargs.pop('account', 'rider@example.invalid'),
            kwargs.pop('password', 'synthetic password'), '0123456789abcdef',
            login=self.login, client_factory=self.factory, clock=lambda: self.now, **kwargs)

    def test_app_wire_protocol_and_response(self):
        for expiry in (self.now + 3600, (self.now + 3600) * 1000):
            payload = {'code': 200, 'data': {'token': 'synthetic-token', 'expiredTimeStamp': expiry,
                                          'user': {'email': 'must not persist'}}}
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps(payload).encode()
            response.headers = {}
            with patch('urllib.request.OpenerDirector.open', return_value=response) as opened:
                token, seconds = password_login('rider@example.invalid', ' spaces $ä ', '0123456789abcdef')
            self.assertEqual((token, seconds), ('synthetic-token', self.now + 3600))
            request = opened.call_args.args[0]
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.full_url, 'https://msbg.bafang-e.com.cn:8096/BaFangCyclingApi/login/10001')
            fields = urllib.parse.parse_qs(request.data.decode())
            self.assertEqual(fields, {'account': ['rider@example.invalid'],
                'password': [hashlib.md5(' spaces $ä '.encode(), usedforsecurity=False).hexdigest()]})
            self.assertEqual(request.get_header('Userdeviceid'), '0123456789abcdef')
            self.assertEqual(request.get_header('Appclient'), 'com.momasbikes.ebike')

    def test_login_errors_redacted_and_invalid_schema(self):
        for payload, error in (
            ({'code': 202, 'message': 'private password'}, AuthenticationError),
            ({'code': 403, 'message': 'private password'}, AuthenticationError),
            ({'code': 500, 'message': 'private password'}, ApiError),
            ({'code': 200, 'data': {'token': 'private password', 'expiredTimeStamp': self.now}}, ApiError),
            ({'code': 200, 'data': {'token': 'token', 'expiredTimeStamp': True}}, ApiError),
            ({'code': 200, 'data': {'token': 'token', 'expiredTimeStamp': 'invalid'}}, ApiError),
            ({'code': 200}, ApiError), ([], ApiError),
        ):
            with patch('momas_sync.auth.request_bytes', return_value=json.dumps(payload).encode()):
                with self.assertRaises(error) as caught:
                    password_login('rider@example.invalid', 'private password', 'device')
                self.assertNotIn('private password', str(caught.exception))

    def test_transport_rejects_redirect_and_sanitizes_credentials(self):
        for code, error in ((302, ApiError), (401, AuthenticationError), (429, TransientError)):
            exception = urllib.error.HTTPError('https://private/?token=secret', code, 'password', {}, None)
            with patch('urllib.request.OpenerDirector.open', side_effect=exception):
                with self.assertRaises(error) as caught:
                    password_login('rider@example.invalid', 'password', 'device')
                self.assertNotIn('password', str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))

    def test_cache_reused_after_restart_and_private(self):
        self.session().list_page(1)
        self.session().download_gpx('anonymous-ride')
        self.login.assert_called_once()
        path = self.root / 'auth/session.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        content = path.read_text()
        self.assertNotIn('synthetic password', content)
        self.assertNotIn('rider@example.invalid', content)
        self.assertNotIn('user', json.loads(content))
        self.factory.assert_called_with('synthetic-token', '0123456789abcdef', timeout=30)

    def test_renew_before_expiry(self):
        session = self.session()
        session.list_page(1)
        self.now += 3541
        self.login.return_value = ('renewed', self.now + 3600)
        session.list_page(2)
        self.assertEqual(self.login.call_count, 2)
        self.assertEqual(json.loads(session.path.read_text())['token'], 'renewed')

    def test_one_relogin_and_same_operation_retried(self):
        self.client.list_page.side_effect = [AuthenticationError('rejected'), {'code': 200, 'data': []}]
        self.session().list_page(2, 20)
        self.assertEqual(self.login.call_count, 2)
        self.assertEqual(self.client.list_page.call_count, 2)
        self.client.list_page.assert_called_with(2, 20)

    def test_second_token_rejection_blocks_across_restarts(self):
        self.client.list_page.side_effect = AuthenticationError('rejected')
        for _ in range(3):
            with self.assertRaises(AuthenticationError):
                self.session().list_page(1)
        self.assertEqual(self.login.call_count, 2)
        self.assertNotIn('token', json.loads((self.root / 'auth/session.json').read_text()))

    def test_invalid_credentials_block_until_changed(self):
        self.login.side_effect = AuthenticationError('rejected')
        for _ in range(3):
            with self.assertRaises(AuthenticationError):
                self.session().list_page(1)
        self.login.assert_called_once()
        self.login.side_effect = None
        self.session(password='changed synthetic password').list_page(1)
        self.assertEqual(self.login.call_count, 2)

    def test_account_change_never_reuses_other_accounts_token(self):
        self.session().list_page(1)
        self.session(account='another@example.invalid').list_page(1)
        self.assertEqual(self.login.call_count, 2)

    def test_network_failure_does_not_block_login(self):
        self.login.side_effect = [TransientError('network'), ('token', self.now + 3600)]
        with self.assertRaises(TransientError):
            self.session().list_page(1)
        self.session().list_page(1)
        self.assertEqual(self.login.call_count, 2)

    def test_expired_login_response_blocks_loop(self):
        self.login.return_value = ('expired', self.now)
        for _ in range(2):
            with self.assertRaises(AuthenticationError):
                self.session().list_page(1)
        self.login.assert_called_once()

    def test_damaged_cache_fails_without_login(self):
        self.session().list_page(1)
        (self.root / 'auth/session.json').write_text('{damaged')
        with self.assertRaises(ApiError):
            self.session()
        self.login.assert_called_once()

    def test_password_file_preserves_spaces_and_dollar(self):
        path = self.root / 'password'
        path.write_bytes(b' space $password \r\n')
        with patch.dict(os.environ, {'BAFANG_PASSWORD_FILE': str(path)}):
            self.assertEqual(secret_value('BAFANG_PASSWORD', password=True), ' space $password ')

    def test_device_id_is_generated_once_and_configurable(self):
        with patch.dict(os.environ, {'BAFANG_DEVICE_ID': ''}):
            generated = device_id(self.root)
            self.assertEqual(len(generated), 16)
            self.assertEqual(device_id(self.root), generated)
        with patch.dict(os.environ, {'BAFANG_DEVICE_ID': 'configured-device'}):
            self.assertEqual(device_id(self.root), 'configured-device')
        self.assertEqual((self.root / 'auth/device_id').stat().st_mode & 0o777, 0o600)

    def test_login_configuration_never_falls_back_to_token(self):
        env = {'BAFANG_AUTH_MODE': 'login', 'BAFANG_ACCOUNT': 'rider@example.invalid',
               'BAFANG_PASSWORD': '', 'BAFANG_ACCOUNT_FILE': '', 'BAFANG_PASSWORD_FILE': '',
               'BAFANG_TOKEN': 'legacy token', 'BAFANG_DEVICE_ID': 'device'}
        with patch.dict(os.environ, env):
            with self.assertRaises(ValueError):
                Settings(data_dir=self.root).client()

    def test_cli_login_only_and_reset(self):
        env = {'DATA_DIR': str(self.root)}
        with patch.dict(os.environ, env), patch.object(Settings, 'client', return_value=self.session()), \
             patch('sys.argv', ['worker', 'login-test']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertIn('login_verified', output.getvalue())
        self.assertNotIn('synthetic-token', output.getvalue())
        self.assertFalse((self.root / 'sync.sqlite3').exists())
        with patch.dict(os.environ, env), patch('sys.argv', ['worker', 'auth-reset']), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertFalse((self.root / 'auth/session.json').exists())


if __name__ == '__main__':
    unittest.main()
