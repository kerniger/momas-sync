from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET
from momas_sync.database import Store
from momas_sync.endurain import gpx_statistics, upload_ready, validate_tcx_response, UploadUnknown
from momas_sync.prepare import prepare_gpx
from momas_sync.settings import Settings
from momas_sync.sync import Synchronizer
from momas_sync.tcx import to_tcx, TCX, EXT

FIXTURES=Path(__file__).parent/'fixtures'


class TcxTests(unittest.TestCase):
    def setUp(self):
        self.ride=json.loads((FIXTURES/'list.json').read_text())['data']['records'][0]
        self.gpx,_=prepare_gpx(self.ride,(FIXTURES/'ride.gpx').read_bytes(),'Europe/Zurich')

    def test_conversion_preserves_zero_power_time_position_and_cadence(self):
        gpx=self.gpx.replace(b'<power>120</power>',b'<power>0</power>')
        tree=ET.fromstring(to_tcx(self.ride,gpx))
        points=tree.findall(f'.//{{{TCX}}}Trackpoint')
        self.assertEqual(len(points),2)
        self.assertEqual(points[0].find(f'{{{TCX}}}Time').text,'2026-10-09T10:00:00Z')
        self.assertEqual(points[0].find(f'{{{TCX}}}Position/{{{TCX}}}LatitudeDegrees').text,'0')
        self.assertEqual(points[0].find(f'{{{TCX}}}Cadence').text,'70')
        self.assertEqual(points[0].find(f'{{{TCX}}}Extensions/{{{EXT}}}TPX/{{{EXT}}}Watts').text,'0')
        self.assertEqual(tree.find(f'.//{{{TCX}}}DistanceMeters').text,'4123.0')
        self.assertEqual(tree.find(f'.//{{{TCX}}}Activity').attrib['Sport'],'Biking')

    def test_invalid_sensor_not_silently_truncated(self):
        for value in (b'nan',b'-1',b'120.5'):
            with self.assertRaises(ValueError):
                to_tcx(self.ride,self.gpx.replace(b'<power>120</power>',b'<power>'+value+b'</power>'))

    def test_response_time_zone_and_zero_inclusive_average(self):
        response={'average_power':120,'max_power':120,'average_cad':70,'max_cad':70,
                  'distance':4123,'timezone':'Europe/Zurich','start_time_tz_applied':'2026-10-09T12:00:00',
                  'end_time_tz_applied':'2026-10-09T12:10:00','total_elapsed_time':600}
        self.assertTrue(validate_tcx_response(self.ride,self.gpx,response)['summary_verified'])
        response['average_power']=999
        self.assertFalse(validate_tcx_response(self.ride,self.gpx,response)['summary_verified'])

    def test_automatic_upload_is_tcx_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=Settings(data_dir=Path(temp))
            store=Store(settings.data_dir)
            try:
                bafang=Mock()
                bafang.list_page.side_effect=lambda page,size: {'data':[self.ride]} if page==1 else {'data':[]}
                bafang.download_gpx.return_value=(FIXTURES/'ride.gpx').read_bytes()
                Synchronizer(settings,store,lambda:bafang).cycle()
                client=Mock();client.url='http://192.0.2.1:8080'
                client.upload.return_value=[{'id':1,'average_power':120,'max_power':120,'average_cad':70,'max_cad':70,
                    'distance':4123,'timezone':'Europe/Zurich','start_time_tz_applied':'2026-10-09T12:00:00',
                    'end_time_tz_applied':'2026-10-09T12:10:00','total_elapsed_time':600}]
                first=upload_ready(settings,store,client)
                self.assertEqual(first['uploaded'],1)
                self.assertEqual(first['upload_errors'],0)
                self.assertEqual(client.upload.call_args.kwargs['file_type'],'tcx')
                self.assertEqual(store.upload_record(client.url,self.ride['id'])['status'],'verified')
                second=upload_ready(settings,store,client)
                self.assertEqual(second['upload_skipped'],1)
                client.upload.assert_called_once()
            finally:store.close()

    def test_uncertain_post_blocks_future_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=Settings(data_dir=Path(temp));store=Store(settings.data_dir)
            try:
                bafang=Mock()
                bafang.list_page.side_effect=lambda page,size: {'data':[self.ride]} if page==1 else {'data':[]}
                bafang.download_gpx.return_value=(FIXTURES/'ride.gpx').read_bytes()
                Synchronizer(settings,store,lambda:bafang).cycle()
                client=Mock();client.url='http://192.0.2.1:8080';client.upload.side_effect=UploadUnknown('timeout')
                self.assertEqual(upload_ready(settings,store,client)['upload_errors'],1)
                self.assertEqual(upload_ready(settings,store,client)['upload_blocked'],1)
                client.upload.assert_called_once()
            finally:store.close()


if __name__=='__main__':unittest.main()
