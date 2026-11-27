import base64
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from app import personal_cloud_images as images


class ImageMultipartTests(unittest.IsolatedAsyncioTestCase):
    origin = 'https://example.test'
    name = '验收样本 & 复查.png'
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII=')

    async def asyncSetUp(self):
        actor = SimpleNamespace(user_no='synthetic')
        self.identity = AsyncMock(return_value=actor)
        self.enterContext(patch.object(images.cloud, 'actor_for', self.identity))
        self.enterContext(patch.object(images.cloud, 'touch_host', AsyncMock()))
        self.enterContext(patch.object(images.iam.IAMConfig, 'from_env',
                                       return_value=SimpleNamespace(public_origin=self.origin)))
        app = FastAPI()
        app.include_router(images.router)

        @app.exception_handler(images.iam.IAMFailure)
        async def failure(request, exc):
            return JSONResponse({'detail': exc.code}, status_code=exc.status)

        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            base_url=self.origin, transport=httpx.ASGITransport(app=app)))

    async def post(self, size=1024, origin=None, **kwargs):
        return await self.client.post('/v1/personal-cloud/web/image',
                                      params={'filename': self.name, 'size': str(size)},
                                      headers={'Origin': origin or self.origin, **kwargs.pop('headers', {})}, **kwargs)

    def image(self, data):
        return {'file': (self.name, data, 'image/png')}

    async def test_old_and_new_carriers_return_identical_image_after_gateway_resizing(self):
        expected = {'mediaType': 'image/png', 'name': self.name, 'data': base64.b64encode(self.png).decode()}
        for kwargs in ({'content': self.png, 'headers': {'Content-Type': 'application/octet-stream'}},
                       {'files': self.image(self.png)}):
            response = await self.post(**kwargs)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), expected)
        self.assertEqual(self.identity.await_count, 4)

    async def test_recognizes_remaining_ciphertext_after_unwrapping(self):
        response = await self.post(files=self.image(b'%TSD-Header-###%fixture'))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {'detail': 'personal_cloud_image_still_encrypted'})

    async def test_extra_parts_wrong_type_and_empty_rejected(self):
        response = await self.post(files=[('file', (self.name, self.png)), ('file', ('b.png', self.png))])
        self.assertEqual(response.status_code, 400)
        for payload in (b'', b'<svg/>'):
            self.assertEqual((await self.post(files=self.image(payload))).status_code, 422)

    async def test_image_quota_excludes_valid_boundary_but_limits_wire_and_file(self):
        size = len(self.png)
        with patch.object(images, 'MAX_IMAGE_BYTES', size):
            for payload, expected in [(self.png, 200), (self.png + b'x', 413), (self.png + bytes(9000), 413)]:
                with self.subTest(length=len(payload)):
                    self.assertEqual((await self.post(size=size, files=self.image(payload))).status_code, expected)
            self.assertEqual((await self.post(size=size+1, files=self.image(self.png))).status_code, 413)

    async def test_recipient_rechecked_and_foreign_origin_refused(self):
        self.assertEqual((await self.post(files=self.image(self.png), origin='https://other.test')).status_code, 403)
        self.identity.side_effect = [SimpleNamespace(user_no='A'), SimpleNamespace(user_no='B')]
        response = await self.post(files=self.image(self.png))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('data', response.json())
