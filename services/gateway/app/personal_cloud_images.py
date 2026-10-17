"""Authenticated web image upload through the enterprise gateway's URL protocol.

Only the receiving bytes are returned to the same signed-in browser. No files,
keys, external URLs, or business-image logs are created. DSH remains responsible
for full image decoding, limits, durable attachment admission and model history.
"""
import base64
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import iam_oauth as iam
from . import personal_cloud as cloud

router = APIRouter()
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def image_media_type(data: bytes) -> str:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'%TSD-Header-###%'):
        raise iam.IAMFailure(422, 'personal_cloud_image_still_encrypted')
    raise iam.IAMFailure(422, 'personal_cloud_image_type_unsupported')


@router.post('/v1/personal-cloud/web/image')
async def upload_image(request: Request):
    if request.headers.get('authorization'):
        raise iam.IAMFailure(403, 'web_session_required')
    actor = await cloud.actor_for(request)
    if request.headers.get('origin') != iam.IAMConfig.from_env().public_origin:
        raise iam.IAMFailure(403, 'iam_origin_denied')
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
        raise iam.IAMFailure(415, 'personal_cloud_image_invalid')
    names, sizes = request.query_params.getlist('filename'), request.query_params.getlist('size')
    if len(names) != 1 or len(sizes) != 1:
        raise iam.IAMFailure(400, 'personal_cloud_image_invalid')
    name, original_size = names[0], sizes[0]
    if (not name or len(name) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or not re.fullmatch(r'[1-9][0-9]{0,8}', original_size)):
        raise iam.IAMFailure(400, 'personal_cloud_image_invalid')
    if int(original_size) > MAX_IMAGE_BYTES:
        raise iam.IAMFailure(413, 'personal_cloud_image_too_large')
    # The declared size is the ciphertext sent by the browser. A decrypting
    # gateway can change it; never require equality with the received image.
    await cloud.touch_host(request, actor, activity=True)
    progress = time.monotonic()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_IMAGE_BYTES:
            raise iam.IAMFailure(413, 'personal_cloud_image_too_large')
        body.extend(chunk)
        if chunk and time.monotonic() - progress >= 15:
            await cloud.touch_host(request, actor, activity=True)
            progress = time.monotonic()
    if not body:
        raise iam.IAMFailure(422, 'personal_cloud_image_invalid')
    media_type = image_media_type(body)
    current = await cloud.actor_for(request)
    if current.user_no != actor.user_no:
        raise iam.IAMFailure(401, 'personal_cloud_identity_changed')
    return JSONResponse({'mediaType': media_type, 'data': base64.b64encode(body).decode('ascii'), 'name': name},
                        headers={**cloud.HEADERS, 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})
