import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from momas_sync.backup import create_backup, verify_backup
from momas_sync.client import ApiError, AuthenticationError, BafangClient, TransientError
from momas_sync.database import AlreadyRunning, Store, writer_lock
from momas_sync.health import healthy
from momas_sync.prepare import TimeReview, prepare_gpx
from momas_sync.retry import Cancelled, Retrier
from momas_sync.service import log, serve
from momas_sync.settings import Settings
from momas_sync.storage import digest
from momas_sync.sync import Synchronizer
from momas_sync.timestamps import analyze, parse_gpx, parse_time

FIXTURES = Path(__file__).parent / 'fixtures'


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'sync'
        self.settings = Settings(data_dir=self.root)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.payload = json.loads((FIXTURES / 'list.json').read_text())
        self.gpx = (FIXTURES / 'ride.gpx').read_bytes()
        self.ride = self.payload['data']['records'][0]
        self.client = Mock()
        self.client.list_page.side_effect = lambda page, size: self.payload if page == 1 else {'code':200,'data':{'records':[]}}
        self.client.download_gpx.return_value = self.gpx
        self.worker = Synchronizer(self.settings, self.store, lambda: self.client)
        self.worker.retrier.wait = Mock()

    def test_restart_deduplicates_and_retains_original_bytes(self):
        first = self.worker.cycle()
        self.assertEqual(first['status'],'ok')
        self.assertEqual(first['counts']['downloaded'],1)
        row = self.store.get(self.ride['id'])
        self.assertEqual((self.root/row['raw_path']).read_bytes(),self.gpx)
        self.assertEqual(row['correction_seconds'],-28800)
        restored = Store(self.root)
        try:
            second = Synchronizer(self.settings,restored,lambda: self.client).cycle()
            self.assertEqual(second['counts']['skipped'],1)
            self.assertEqual(second['counts']['downloaded'],0)
            self.assertEqual(len(restored.rides()),1)
        finally:
            restored.close()
        self.client.download_gpx.assert_called_once()

    def test_timestamp_copy_preserves_track_data_and_extensions(self):
        prepared, check = prepare_gpx(self.ride,self.gpx,'Europe/Zurich')
        self.assertEqual(check['correction_seconds'],-28800)
        comparison = analyze(self.ride,prepared,'Europe/Zurich')
        self.assertEqual(comparison['start_delta_seconds'],0)
        self.assertEqual(comparison['end_delta_seconds'],0)
        original, copy = parse_gpx(self.gpx), parse_gpx(prepared)
        a=[(node.tag,node.attrib,node.text) for node in original.iter() if node.tag.split('}')[-1] != 'time']
        b=[(node.tag,node.attrib,node.text) for node in copy.iter() if node.tag.split('}')[-1] != 'time']
        self.assertEqual(a,b)

    def test_already_correct_gpx_not_shifted_twice(self):
        corrected,_=prepare_gpx(self.ride,self.gpx,'Europe/Zurich')
        second,check=prepare_gpx(self.ride,corrected,'Europe/Zurich')
        self.assertEqual(second,corrected)
        self.assertEqual(check['correction_seconds'],0)

    def test_unrecognized_offset_requires_review_without_export(self):
        self.client.download_gpx.return_value = self.gpx.replace(b'T18:',b'T17:')
        summary=self.worker.cycle()
        row=self.store.get(self.ride['id'])
        self.assertEqual(summary['status'],'degraded')
        self.assertEqual(row['status'],'review')
        self.assertIsNone(row['export_path'])
        self.assertFalse(healthy(self.settings))

    def test_both_boundaries_required_for_correction(self):
        with self.assertRaises(TimeReview):
            prepare_gpx(self.ride,self.gpx.replace(b'T18:10:',b'T19:10:'),'Europe/Zurich')

    def test_missing_or_naive_trackpoint_time_requires_review(self):
        for broken in (self.gpx.replace(b'<time>2026-10-09T18:00:00Z</time>',b''),self.gpx.replace(b'Z</time>',b'</time>')):
            with self.assertRaises(TimeReview):
                prepare_gpx(self.ride,broken,'Europe/Zurich')

    def test_dst_ambiguity_and_nonexistent_time_not_guessed(self):
        for value in ('2026-10-25 02:30:00','2026-03-29 02:30:00'):
            with self.assertRaises(ValueError):
                parse_time(value,'Europe/Zurich')

    def test_transient_retry_then_success(self):
        self.client.download_gpx.side_effect = [TransientError('sensitive URL'),self.gpx]
        summary=self.worker.cycle()
        self.assertEqual(summary['status'],'ok')
        self.assertEqual(self.client.download_gpx.call_count,2)
        self.worker.retrier.wait.assert_called_once()

    def test_failed_download_resumes_next_cycle(self):
        self.client.download_gpx.side_effect = TransientError('private')
        first=self.worker.cycle()
        self.assertEqual(first['status'],'degraded')
        self.assertEqual(self.store.get(self.ride['id'])['status'],'error')
        self.assertEqual(self.client.download_gpx.call_count,4)
        self.client.download_gpx.side_effect=None
        second=self.worker.cycle()
        self.assertEqual(second['status'],'ok')
        self.assertEqual(self.store.get(self.ride['id'])['status'],'ready')
        self.assertEqual(len(self.store.rides()),1)

    def test_auth_error_not_retried_and_does_not_erase_last_success(self):
        self.worker.cycle()
        success=self.store.state()['last_success']
        self.client.list_page.side_effect=AuthenticationError('private')
        self.client.list_page.reset_mock()
        summary=self.worker.cycle()
        self.assertEqual(summary['status'],'auth_error')
        self.assertEqual(self.client.list_page.call_count,1)
        self.assertEqual(self.store.state()['last_success'],success)
        self.assertFalse(healthy(self.settings))

    def test_corrupt_original_redownloaded(self):
        self.worker.cycle()
        row=self.store.get(self.ride['id'])
        (self.root/row['raw_path']).write_bytes(b'corrupt')
        summary=self.worker.cycle()
        self.assertEqual(summary['counts']['downloaded'],1)
        self.assertEqual((self.root/row['raw_path']).read_bytes(),self.gpx)

    def test_missing_export_rebuilt_without_download(self):
        self.worker.cycle()
        row=self.store.get(self.ride['id'])
        (self.root/row['export_path']).unlink()
        summary=self.worker.cycle()
        self.assertEqual(summary['counts']['downloaded'],0)
        self.assertEqual(summary['counts']['prepared'],1)
        self.assertTrue((self.root/row['export_path']).exists())

    def test_metadata_revision_invalidates_export_and_keeps_originals(self):
        self.worker.cycle()
        old=self.store.get(self.ride['id'])
        self.ride['trip']=5.123
        summary=self.worker.cycle()
        new=self.store.get(self.ride['id'])
        self.assertEqual(summary['counts']['new'],0)
        self.assertEqual(summary['counts']['downloaded'],1)
        self.assertNotEqual(old['metadata_sha'],new['metadata_sha'])
        self.assertTrue((self.root/old['metadata_path']).exists())
        self.assertTrue((self.root/new['metadata_path']).exists())

    def test_pagination_limit_not_reported_as_success(self):
        self.worker.settings=replace(self.settings,max_pages=1)
        summary=self.worker.cycle()
        self.assertEqual(summary['status'],'degraded')
        self.assertEqual(summary['error_code'],'pagination_limit')

    def test_health_requires_success_and_recent_heartbeat(self):
        self.assertFalse(healthy(self.settings))
        self.worker.cycle()
        self.assertTrue(healthy(self.settings))
        self.assertFalse(healthy(self.settings,now=time.time()+200))
        with self.store.connection:
            self.store.connection.execute('UPDATE state SET last_success=?',(time.time()-4000,))
        self.assertFalse(healthy(self.settings))

    def test_only_one_writer(self):
        with writer_lock(self.root):
            with self.assertRaises(AlreadyRunning):
                with writer_lock(self.root):
                    pass

    def test_backup_restore_hashes_and_deduplication(self):
        self.worker.cycle()
        destination=Path(self.temp.name)/'backup'
        create_backup(self.root,destination)
        verify_backup(destination)
        restored=Path(self.temp.name)/'restored'
        shutil.copytree(destination,restored)
        restored_store=Store(restored)
        try:
            summary=Synchronizer(replace(self.settings,data_dir=restored),restored_store,lambda:self.client).cycle()
            self.assertEqual(summary['counts']['downloaded'],0)
            self.assertEqual(summary['counts']['skipped'],1)
        finally:
            restored_store.close()
        (destination/'status.json').write_text('corrupt')
        with self.assertRaises(ValueError):
            verify_backup(destination)

    def test_backup_requires_writer_stopped(self):
        with writer_lock(self.root),self.assertRaises(AlreadyRunning):
            create_backup(self.root,Path(self.temp.name)/'backup')

    def test_private_errors_not_logged(self):
        private='token=TOPSECRET latitude=123.456'
        self.client.list_page.side_effect=TransientError(private)
        self.worker.log=log
        self.worker.retrier.log=log
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.worker.cycle()
        self.assertNotIn('TOPSECRET',output.getvalue())
        self.assertNotIn('123.456',output.getvalue())
        self.assertNotIn(self.ride['id'],output.getvalue())

    def test_service_runs_repeated_cycles(self):
        stop=threading.Event()
        waits=[]
        def wait(seconds):
            waits.append(seconds)
            if len(waits)==2:
                stop.set()
        with patch('momas_sync.service.log'), patch.object(Retrier,'wait',side_effect=wait):
            self.assertEqual(serve(self.settings,stop=stop,client_factory=lambda:self.client),0)
        self.assertEqual(waits,[1800,1800])
        self.assertEqual(self.store.state()['cycles'],2)
        self.client.download_gpx.assert_called_once()

    def test_graceful_shutdown_during_wait(self):
        stop=threading.Event()
        def finish_cycle(**fields):
            if fields.get('status') == 'ok':
                stop.set()
        with patch('momas_sync.service.log',side_effect=lambda event,**fields: finish_cycle(**fields)):
            self.assertEqual(serve(self.settings,stop=stop,client_factory=lambda:self.client),0)
        self.assertEqual(self.store.state()['status'],'stopped')


class RetryAndClientTests(unittest.TestCase):
    def test_backoff_is_capped_and_cancelable(self):
        settings=Settings(retry_attempts=4,retry_base=2,retry_cap=5)
        retry=Retrier(settings,jitter=lambda low,high:high)
        retry.wait=Mock()
        operation=Mock(side_effect=TransientError('do not print'))
        with self.assertRaises(TransientError):
            retry.call(operation)
        self.assertEqual([call.args[0] for call in retry.wait.call_args_list],[2,4,5])
        stop=threading.Event();stop.set()
        retry=Retrier(settings,stop=stop)
        with self.assertRaises(Cancelled):
            retry.wait(20)

    def test_long_retry_after_respected_with_cancelable_wait(self):
        retry=Retrier(Settings())
        operation=Mock(side_effect=[TransientError('rate limited',retry_after=120),'ok'])
        retry.wait=Mock()
        self.assertEqual(retry.call(operation),'ok')
        retry.wait.assert_called_once_with(120)

    def test_http_429_and_500_retryable_400_not_retryable(self):
        for code in (429,500,400):
            error=urllib.error.HTTPError('https://example/?token=secret',code,'private',{'Retry-After':'3'},None)
            with patch('urllib.request.OpenerDirector.open',side_effect=error):
                with self.assertRaises(ApiError) as raised:
                    BafangClient('secret','device').list_page(1)
                self.assertEqual(isinstance(raised.exception,TransientError),code!=400)
                if code!=400:
                    self.assertEqual(raised.exception.retry_after,3)

    def test_token_rejection_in_gpx_response(self):
        with patch.object(BafangClient,'_get',return_value=b'{"code":401,"message":"secret"}'):
            with self.assertRaises(AuthenticationError):
                BafangClient('secret','device').download_gpx('anonymous')

    def test_healthcheck_missing_database_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=Settings(data_dir=Path(temp)/'missing')
            self.assertFalse(healthy(settings))
            self.assertFalse(settings.data_dir.exists())

    def test_invalid_settings_rejected(self):
        for kwargs in ({'interval':0},{'timeout':float('nan')},{'retry_attempts':0},{'retry_base':100},{'health_stale':10}):
            with self.assertRaises(ValueError):
                Settings(**kwargs)


if __name__ == '__main__':
    unittest.main()
