import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from momas_sync.client import BafangClient, ApiError, AuthenticationError, extract_rides
from momas_sync.timestamps import analyze, parse_time, parse_gpx
from momas_sync.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


class PocTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((FIXTURES / "list.json").read_text())
        self.gpx = (FIXTURES / "ride.gpx").read_bytes()

    def test_real_schema_and_errors(self):
        self.assertEqual(extract_rides(self.payload)[0]['id'], 'anonymous-001')
        for invalid in ({"code":401,"message":"secret"}, {"data":{}}, {"data":[{}]}, {"success":False}):
            with self.assertRaises(ApiError):
                extract_rides(invalid)

    def test_timestamp_shift_and_extensions(self):
        result = analyze(extract_rides(self.payload)[0], self.gpx, 'Europe/Zurich')
        self.assertEqual(result['start_delta_seconds'], 8 * 3600)
        self.assertEqual(result['end_delta_seconds'], 8 * 3600)
        self.assertTrue(result['gpx_has_power'])
        self.assertTrue(result['gpx_has_cadence'])

    def test_epochs_and_dst(self):
        self.assertEqual(parse_time(1791540000, 'Europe/Zurich'), parse_time(1791540000000, 'Europe/Zurich'))
        self.assertEqual(parse_time('2026-01-01 12:00:00', 'Europe/Zurich').hour, 11)
        self.assertEqual(parse_time('2026-07-01 12:00:00', 'Europe/Zurich').hour, 10)
        self.assertEqual(parse_time('2026-07-01T12:00:00Z', 'Europe/Zurich').hour, 12)

    def test_invalid_gpx(self):
        for body in (b'{"code":401}', b'<html/>', b'<!DOCTYPE gpx><gpx/>'):
            with self.assertRaises(ApiError):
                parse_gpx(body)

    def test_request_read_only_and_sanitized_http_error(self):
        error = urllib.error.HTTPError('https://example/?token=secret',401,'secret',{},None)
        with patch('urllib.request.OpenerDirector.open', side_effect=error) as request:
            with self.assertRaises(AuthenticationError) as raised:
                BafangClient('secret','device').list_page(2)
            req = request.call_args.args[0]
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.get_header('Appclient'), 'com.momasbikes.ebike')
            self.assertIn('page=2', req.full_url)
            self.assertNotIn('secret', str(raised.exception))

    def test_export_pagination_originals_and_no_private_logs(self):
        empty = {"code":200,"data":{"records":[]}}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'BAFANG_TOKEN':'secret','BAFANG_DEVICE_ID':'device','BAFANG_TOKEN_FILE':''}), patch('sys.argv', ['poc','--data-dir',temp]), patch.object(BafangClient,'list_page',side_effect=[self.payload,empty]) as listing, patch.object(BafangClient,'download_gpx',return_value=self.gpx), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(),0)
            self.assertEqual(listing.call_args_list[1].args[0],2)
            self.assertEqual(next((Path(temp)/'gpx').glob('*.gpx')).read_bytes(),self.gpx)
            report = json.loads((Path(temp)/'report.json').read_text())
            self.assertTrue(report['pagination_complete'])
            self.assertEqual(report['unique_rides'],1)
            self.assertNotIn('anonymous-001',output.getvalue())
            self.assertNotIn('secret',output.getvalue())

    def test_two_nonempty_pages(self):
        second = {"code":200,"data":{"records":[{"id":"anonymous-002"}]}}
        empty = {"code":200,"data":{"records":[]}}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'BAFANG_TOKEN':'secret','BAFANG_DEVICE_ID':'device','BAFANG_TOKEN_FILE':''}), patch('sys.argv',['poc','--data-dir',temp,'--max-downloads','0']), patch.object(BafangClient,'list_page',side_effect=[self.payload,second,empty]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(),0)
            report = json.loads((Path(temp)/'report.json').read_text())
            self.assertEqual(report['unique_rides'],2)
            self.assertEqual(report['pages'],3)
            self.assertTrue(report['pagination_complete'])

    def test_application_authentication_error(self):
        with self.assertRaises(AuthenticationError):
            extract_rides({"code":401,"message":"do not log"})

    def test_naive_gpx_not_assumed_utc(self):
        result = analyze(extract_rides(self.payload)[0], self.gpx.replace(b'Z</time>',b'</time>'), 'Europe/Zurich')
        self.assertTrue(result['gpx_has_naive_times'])
        self.assertIsNone(result['start_delta_seconds'])

    def test_repeated_page_fails_safely(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'BAFANG_TOKEN':'secret','BAFANG_DEVICE_ID':'device','BAFANG_TOKEN_FILE':''}), patch('sys.argv',['poc','--data-dir',temp,'--max-downloads','0']), patch.object(BafangClient,'list_page',return_value=self.payload), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(),1)


if __name__ == '__main__':
    unittest.main()
