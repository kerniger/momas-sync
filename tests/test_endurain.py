import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
from momas_sync.backup import create_backup, verify_backup
from momas_sync.database import Store
from momas_sync.endurain import EndurainClient, UploadError, UploadRejected, UploadUnknown, gpx_statistics, test_upload
from momas_sync.settings import Settings
from momas_sync.sync import Synchronizer

FIXTURES=Path(__file__).parent/'fixtures'


class EndurainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings=Settings(data_dir=Path(self.temp.name)/'sync')
        self.store=Store(self.settings.data_dir)
        self.addCleanup(self.store.close)
        self.gpx=(FIXTURES/'ride.gpx').read_bytes()
        bafang=Mock()
        payload=json.loads((FIXTURES/'list.json').read_text())
        bafang.list_page.side_effect=lambda page,size: payload if page==1 else {'code':200,'data':{'records':[]}}
        bafang.download_gpx.return_value=self.gpx
        self.assertEqual(Synchronizer(self.settings,self.store,lambda:bafang).cycle()['status'],'ok')
        self.client=Mock()
        self.client.url='http://192.0.2.1:8080'
        self.client.upload.return_value=[{'id':123,'average_power':120,'average_cad':70}]

    def test_upload_once_persisted_before_post_and_skipped_on_repeat(self):
        def uploaded(body):
            record=self.store.upload_record(self.client.url,'anonymous-001')
            self.assertEqual(record['status'],'inflight')
            return [{'id':123}]
        self.client.upload.side_effect=uploaded
        self.assertEqual(test_upload(self.settings,self.store,self.client)['uploaded'],1)
        self.assertEqual(test_upload(self.settings,self.store,self.client)['skipped'],1)
        self.client.upload.assert_called_once()
        row=self.store.upload_record(self.client.url,'anonymous-001')
        self.assertEqual(row['status'],'uploaded')
        self.assertEqual(row['activity_id'],123)
        backup=Path(self.temp.name)/'backup'
        create_backup(self.settings.data_dir,backup)
        verify_backup(backup)

    def test_uncertain_upload_not_retried(self):
        self.client.upload.side_effect=UploadUnknown('network_failure')
        with self.assertRaises(UploadUnknown):
            test_upload(self.settings,self.store,self.client)
        self.assertEqual(self.store.upload_record(self.client.url,'anonymous-001')['status'],'unknown')
        with self.assertRaises(UploadError):
            test_upload(self.settings,self.store,self.client)
        self.client.upload.assert_called_once()

    def test_rejected_upload_not_silently_retried(self):
        self.client.upload.side_effect=UploadRejected('http_401')
        with self.assertRaises(UploadRejected):
            test_upload(self.settings,self.store,self.client)
        self.assertEqual(self.store.upload_record(self.client.url,'anonymous-001')['status'],'rejected')
        with self.assertRaises(UploadError):
            test_upload(self.settings,self.store,self.client)

    def test_corrupt_export_not_sent(self):
        row=self.store.get('anonymous-001')
        (self.settings.data_dir/row['export_path']).write_bytes(b'corrupt')
        with self.assertRaises(UploadError):
            test_upload(self.settings,self.store,self.client)
        self.client.upload.assert_not_called()

    def test_gpx_statistics_extension_values(self):
        stats=gpx_statistics(self.gpx)
        self.assertEqual(stats['trackpoints'],2)
        self.assertEqual(stats['power_samples'],1)
        self.assertEqual(stats['power_nonzero_average'],120)
        self.assertEqual(stats['cadence_average'],70)

    def test_version_one_migrates_without_losing_rides(self):
        with self.store.connection:
            self.store.connection.execute('DROP TABLE uploads')
            self.store.connection.execute('PRAGMA user_version=1')
        migrated=Store(self.settings.data_dir)
        try:
            self.assertEqual(len(migrated.rides()),1)
            self.assertEqual(migrated.connection.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertIsNone(migrated.upload_record(self.client.url,'anonymous-001'))
        finally:
            migrated.close()


class EndurainTransportTests(unittest.TestCase):
    def test_multipart_header_auth_and_no_token_query(self):
        body=(FIXTURES/'ride.gpx').read_bytes()
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.status=201
        response.read.return_value=b'[{"id":123}]'
        with patch('urllib.request.OpenerDirector.open',return_value=response) as send:
            self.assertEqual(EndurainClient('http://192.0.2.1:8080','secret').upload(body),[{'id':123}])
            request=send.call_args.args[0]
            self.assertEqual(request.get_method(),'POST')
            self.assertEqual(request.get_header('X-api-key'),'secret')
            self.assertNotIn('secret',request.full_url)
            self.assertIn(b'name="file"',request.data)
            self.assertIn(body,request.data)

    def test_http_failures_sanitized_and_no_retries(self):
        body=(FIXTURES/'ride.gpx').read_bytes()
        for code in (401,403,500,409):
            error=urllib.error.HTTPError('http://example/?api_key=secret',code,'GPS private',{},None)
            with patch('urllib.request.OpenerDirector.open',side_effect=error) as send:
                with self.assertRaises(UploadError) as raised:
                    EndurainClient('http://192.0.2.1:8080','secret').upload(body)
                self.assertNotIn('secret',str(raised.exception))
                self.assertNotIn('GPS',str(raised.exception))
                self.assertEqual(isinstance(raised.exception,UploadUnknown),code in (500,409))
                send.assert_called_once()

    def test_response_without_activity_id_requires_reconciliation(self):
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.status=201
        response.read.return_value=b'[]'
        with patch('urllib.request.OpenerDirector.open',return_value=response),self.assertRaises(UploadUnknown):
            EndurainClient('http://192.0.2.1:8080','secret').upload((FIXTURES/'ride.gpx').read_bytes())


if __name__=='__main__':
    unittest.main()
