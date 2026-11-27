"""Gateway regressions for delivered files and decryption diagnostics."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import anyio
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from app import personal_cloud_files as files, storage_api, web_upload

class RecoveryTests(unittest.TestCase):
    def test_native_binary_and_json_file_reads_preserve_exact_bytes(self):
        boundary = b'qa-download'
        media = 'multipart/form-data; boundary="qa-download"'
        metadata = {'attachments': [{'codec': 'bytes', 'part': 'bin', 'path': ['data']}],
                    'result': {'ok': True, 'value': {'data': None}}}
        def part(name, data):
            return (b'--' + boundary + b'\r\nContent-Disposition: form-data; name="' + name
                    + b'"\r\n\r\n' + data + b'\r\n')
        meta = part(b'metadata', json.dumps(metadata).encode())
        closing = b'--' + boundary + b'--\r\n'
        for content in (b'', bytes(range(256)) * 4096, b'\r\n\x00\n', b'embedded--qa-downloadtext'):
            binary = part(b'bin', content)
            for body in (meta + binary + closing, binary + meta + closing):
                self.assertEqual(files.native_response(bytearray(body), media, 'workspaceFiles/readBytes')['result']['value']['data'], content)
        with self.assertRaises(ValueError):
            files.native_response(meta + part(b'bin', b'a') + part(b'extra', b'b') + closing, media, 'workspaceFiles/readBytes')
        payload = {'result': {'value': {'data': base64.b64encode(b'ab').decode()}}}
        self.assertEqual(files.native_response(json.dumps(payload).encode(), 'application/json', 'workspaceFiles/readBytes'), payload)

    def test_disconnect_abandons_download_even_while_asgi_send_is_waiting(self):
        async def disconnect(stage):
            owner, session, file_id = uuid4().hex, 'session-' + str(uuid4()), str(uuid4())
            payload = b'file payload'
            store = Mock()
            store.begin_download.return_value = {
                'expected_bytes': len(payload), 'lease_seconds': 300, 'lease_id': 'download',
                'media_type': 'text/plain', 'name': '下载.txt', 'sha256': hashlib.sha256(payload).hexdigest(),
            }
            store.read_download.return_value = payload
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}}
            sending = anyio.Event()

            async def send(message):
                if message['type'] == stage:
                    sending.set()
                    await anyio.sleep_forever()

            async def receive():
                await sending.wait()
                return {'type': 'http.disconnect'}

            with patch.object(storage_api.cloud, 'actor_for', new=AsyncMock()):
                response = await storage_api.human_download_response(
                    None, owner=owner, session=session, file_id=file_id, store=store,
                )
            with anyio.fail_after(5):
                await response(scope, receive, send)
            store.abandon_download.assert_called_once_with(owner, session, 'download')
            store.finish_download.assert_not_called()

        for stage in ('http.response.start', 'http.response.body'):
            with self.subTest(stage=stage):
                anyio.run(disconnect, stage)

    @unittest.skipUnless(os.name=='posix','Blob verification uses a Linux directory handle')
    def test_large_download_checks_each_block_and_releases_on_change(self):
        from app import storage_lifecycle as storage
        async def exercise(root):
            owner, fid = uuid4().hex, str(uuid4())
            data = b'x' * (storage.DOWNLOAD_BLOCK_BYTES + 5)
            sha = hashlib.sha256(data).hexdigest()
            blob = storage.BlobStore(root)
            blob.stage(owner, fid, data, sha)
            blob.publish(owner, fid, len(data), sha)
            store = Mock(blobs=blob)
            store.begin_download.return_value = {'file_id':fid, 'name':'large.bin', 'expected_bytes':len(data),
                'media_type':'application/octet-stream', 'sha256':sha, 'lease_id':'test', 'lease_seconds':300}
            def read(_owner, _session, _lease, *, block_hashes):
                self.assertTrue(block_hashes)
                return blob.download_hashes(owner, fid, len(data), sha)
            store.read_download.side_effect = read
            with patch.object(storage_api.cloud,'actor_for',new=AsyncMock()):
                result = await storage_api.human_download_response(None, owner=owner, session='qa', file_id=fid, store=store)
            self.assertEqual(int(result.headers['content-length']),len(data))
            self.assertEqual(await anext(result.body_iterator), data[:storage.DOWNLOAD_BLOCK_BYTES])
            with (Path(root)/owner/fid).open('r+b') as edited:
                edited.seek(storage.DOWNLOAD_BLOCK_BYTES)
                edited.write(b'other')
            with self.assertRaises(storage.StorageError):
                await anext(result.body_iterator)
            store.abandon_download.assert_called_once_with(owner,'qa','test')
            store.finish_download.assert_not_called()
        with tempfile.TemporaryDirectory() as root:
            asyncio.run(exercise(root))

    def test_known_relative_output_can_download_without_opening_workspace(self):
        from urllib.parse import urlencode
        sid = 'session-f61fad7d-cf4b-42ff-8667-13a7f391a61d'
        short = 'maimi-outputs/' + sid[8:] + '-6574949b-a8f7-4e7a-b64f-7e7ab511668c/结果.xlsx'
        store = Mock()
        lookup = Mock(return_value='8d08b8cf-61ab-4831-a82e-dc49b73f2d3b')
        request = Request({'type':'http', 'headers':[], 'method':'GET', 'path':'/v1/personal-cloud/web/file',
                           'query_string':urlencode({'session_id':sid,'path':short}).encode()})
        delivered = AsyncMock()
        with (patch.object(files.managed_storage,'configured',return_value=True),
              patch.object(files.managed_storage,'default_store',return_value=store),
              patch('app.restricted_transfer.published',new=lookup),
              patch.object(files.cloud,'actor_for',new=AsyncMock()),
              patch.object(files.cloud,'owner_key',return_value='a'*32),
              patch.object(files.NativeFileReader,'open',new=AsyncMock(side_effect=AssertionError('workspace opened'))),
              patch.object(storage_api,'human_download_response',new=delivered)):
            asyncio.run(files.download_file(request))
        lookup.assert_called_once_with(store, 'a'*32, sid, '/workspace/'+short)
        delivered.assert_awaited_once_with(request, owner='a'*32, session=sid, file_id=lookup.return_value, store=store)

    def test_busy_uploads_keep_a_diagnostic_slot_for_encrypted_files(self):
        with (patch.object(web_upload, '_rate_start',100),
              patch.dict(web_upload._rate_counts,clear=True),
              patch.object(web_upload.time,'monotonic',return_value=101),
              self.assertLogs('megmeet.web-upload', level='WARNING') as rows):
            for _ in range(20):
                web_upload.observe_upload('MULTIPART',True,True,'ZIP','ACCEPTED')
            for _ in range(2):
                web_upload.observe_upload('MULTIPART',True,True,'TSD','STILL_ENCRYPTED')
                web_upload.observe_upload('MULTIPART',True,True,'OTHER','INVALID_MULTIPART')
        self.assertEqual(len(rows.records),6)
        self.assertEqual(sum('result=STILL_ENCRYPTED' in row for row in rows.output),1)
        self.assertEqual(sum('result=INVALID_MULTIPART' in row for row in rows.output),1)

if __name__=='__main__':
    unittest.main()
