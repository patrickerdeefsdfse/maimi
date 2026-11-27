"""Bounded drawing preparation; no provider keys, model calls or source app imports.

The gateway returns only session-owned generated image references. The private
worker bridge reads those images for the employee's selected vision model. Raw
image data never becomes a project result stored in PostgreSQL or a chat tool
result. PDFium and Pillow run in a disposable, resource-limited subprocess.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile

MAX_INPUT = 20 * 1024 * 1024
MAX_PAGES = 3
MAX_PAGE_BYTES = 700 * 1024
MAX_OUTPUT = MAX_PAGES * MAX_PAGE_BYTES
MAX_SIDE = 3200
MAX_PIXELS = 40_000_000
TIMEOUT = 45
VERSION = 'heatcost-drawing-preparation-v2'
PAGE_TEXT_CHARS = 6000
IMAGE_FORMATS = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP', '.bmp': 'BMP', '.tif': 'TIFF', '.tiff': 'TIFF'}
DRAWING_FORMATS = {'.pdf', *IMAGE_FORMATS}
FIELD_KEYS = ('partNumber', 'model', 'material', 'length', 'height', 'width', 'weight', 'finCount', 'holeCount')
PROCESS_KEYS = ('cutting', 'drilling', 'tapping', 'milling', 'cnc', 'cleaning', 'deburr', 'marking', 'anodize')


class DrawingFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DrawingTextUnavailable(DrawingFailure):
    pass


def normalize_extraction(raw):
    """Validate the fixed vision response before the deterministic calculator.

    Model output cannot change rates, billing exemptions or waste assumptions.
    Missing evidence erases a guessed value/operation rather than quoting it.
    Native units are mm/g/pcs; model unit strings are not silently interpreted.
    """
    if not isinstance(raw, dict) or set(raw) != {'fields', 'processes'}:
        raise DrawingFailure('project_drawing_model_schema_invalid')
    fields, processes = raw['fields'], raw['processes']
    if (not isinstance(fields, dict) or set(fields) - set(FIELD_KEYS) or
            not isinstance(processes, dict) or set(processes) - set(PROCESS_KEYS)):
        raise DrawingFailure('project_drawing_model_schema_invalid')
    normalized_fields, normalized_processes = {}, {}
    for key in FIELD_KEYS:
        item = fields.get(key, {'value': None, 'evidence': '', 'confidence': 0})
        if not isinstance(item, dict) or set(item) != {'value', 'evidence', 'confidence'}:
            raise DrawingFailure('project_drawing_model_schema_invalid')
        evidence, confidence, value = item['evidence'], item['confidence'], item['value']
        if (not isinstance(evidence, str) or len(evidence) > 600 or '\x00' in evidence or
                type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise DrawingFailure('project_drawing_model_schema_invalid')
        if key in {'partNumber', 'model', 'material'}:
            if value is not None and (not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value)):
                raise DrawingFailure('project_drawing_model_schema_invalid')
            value = (value.strip() or None) if isinstance(value, str) else None
        elif value is not None:
            maximum = 10000 if key in {'length', 'height', 'width'} else 100000
            minimum = 0 if key in {'finCount', 'holeCount'} else 0.000000001
            if (type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum or
                    (key in {'finCount', 'holeCount'} and int(value) != value)):
                raise DrawingFailure('project_drawing_model_schema_invalid')
        if not evidence.strip():
            value, confidence = None, 0
        normalized_fields[key] = {'value': value, 'evidence': evidence.strip(), 'confidence': confidence}
    for key, item in processes.items():
        if (not isinstance(item, dict) or set(item) != {'enabled', 'evidence'} or type(item['enabled']) is not bool or
                not isinstance(item['evidence'], str) or len(item['evidence']) > 600 or '\x00' in item['evidence']):
            raise DrawingFailure('project_drawing_model_schema_invalid')
        evidence = item['evidence'].strip()
        if evidence:
            normalized_processes[key] = {'enabled': item['enabled'], 'evidence': evidence}
    return {'fields': normalized_fields, 'processes': normalized_processes}


def _encode_image(image):
    from PIL import Image
    if image.width < 1 or image.height < 1 or image.width * image.height > MAX_PIXELS:
        raise DrawingFailure('project_drawing_pixels_exceeded')
    if image.mode == 'RGBA' or 'transparency' in image.info:
        rgba = image.convert('RGBA')
        clean = Image.new('RGB', rgba.size, 'white')
        clean.paste(rgba, mask=rgba.getchannel('A'))
        rgba.close()
    else:
        clean = image.convert('RGB')
    try:
        clean.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        # Keep useful drawing resolution. Do not repeatedly shrink tiny text to
        # meet a transport budget: the user can split/crop a complex drawing.
        for quality in (90, 82, 74):
            output = io.BytesIO()
            clean.save(output, 'JPEG', quality=quality, optimize=True, subsampling=0)
            data = output.getvalue()
            if len(data) <= MAX_PAGE_BYTES:
                return data, clean.width, clean.height
        raise DrawingFailure('project_drawing_preview_too_large')
    finally:
        clean.close()


def _limits():
    # This function runs only inside the fresh child, never in a threaded web
    # process. Linux is the cloud runtime; unsupported runtimes fail closed.
    if sys.platform != 'linux':
        raise DrawingFailure('project_drawing_renderer_platform_unsupported')
    import resource
    for which, limit in ((resource.RLIMIT_AS, 640 * 1024 * 1024), (resource.RLIMIT_CPU, 35),
                         (resource.RLIMIT_FSIZE, 4 * 1024 * 1024), (resource.RLIMIT_NOFILE, 64)):
        resource.setrlimit(which, (limit, limit))
    os.umask(0o077)


def _render_child(source, suffix, destination):
    _limits()
    import warnings
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    if source.stat().st_size > MAX_INPUT:
        raise DrawingFailure('project_drawing_input_too_large')
    pages, total_pages = [], 1

    def save(image, number, text=""):
        text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32).replace("\x7f", "")
        data, width, height = _encode_image(image)
        name = f'page-{number}.jpg'
        (destination / name).write_bytes(data)
        pages.append({'file': name, 'page': number, 'media_type': 'image/jpeg',
                      'width': width, 'height': height, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                      'native_text': text[:PAGE_TEXT_CHARS], 'text_truncated': len(text) > PAGE_TEXT_CHARS})

    if suffix == '.pdf':
        try:
            import pypdfium2 as pdfium
        except ImportError:
            raise DrawingFailure('project_drawing_renderer_missing') from None
        # No form initialization, XFA, JavaScript or embedded-file extraction.
        with pdfium.PdfDocument(source) as document:
            total_pages = len(document)
            if not 1 <= total_pages <= 1000:
                raise DrawingFailure('project_drawing_page_count_invalid')
            for index in range(min(MAX_PAGES, total_pages)):
                page = document[index]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(v) and 0 < v <= 100000 for v in (width, height)):
                        raise DrawingFailure('project_drawing_page_size_invalid')
                    scale = min(150 / 72, MAX_SIDE / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            text = ''
                            try:
                                textpage = page.get_textpage()
                                try:
                                    text = textpage.get_text_range(0, min(textpage.count_chars(), PAGE_TEXT_CHARS + 1))
                                finally:
                                    textpage.close()
                            except Exception:
                                pass  # Local text layer is supplementary; OCR and image remain available.
                            save(image, index + 1, text)
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    else:
        expected = IMAGE_FORMATS.get(suffix)
        with Image.open(source) as original:
            total_pages = getattr(original, 'n_frames', 1)
            if not expected or original.format != expected or (expected != 'TIFF' and total_pages != 1):
                raise DrawingFailure('project_drawing_format_invalid')
            if not 1 <= total_pages <= 1000:
                raise DrawingFailure('project_drawing_page_count_invalid')
            for index in range(min(MAX_PAGES, total_pages)):
                original.seek(index)
                if original.width * original.height > MAX_PIXELS:
                    raise DrawingFailure('project_drawing_pixels_exceeded')
                image = ImageOps.exif_transpose(original)
                try:
                    save(image, index + 1)
                finally:
                    image.close()
    metadata = {'version': VERSION, 'pages': pages, 'total_pages': total_pages,
                'truncated': total_pages > MAX_PAGES}
    (destination / 'result.json').write_text(json.dumps(metadata, separators=(',', ':')), encoding='utf-8')


async def _join_without_recancel(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _render(source, suffix, destination):
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(destination),
                   'TMPDIR': str(destination), 'PYTHONNOUSERSITE': '1'}
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable, '-I', str(Path(__file__).resolve()),
                '--render', str(source), suffix, str(destination), env=environment,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
    try:
        child = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        # Cancellation can arrive while process creation has succeeded but the
        # Process object has not yet been returned to us. Join that handoff too.
        child = await _join_without_recancel(spawning)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await _join_without_recancel(asyncio.create_task(child.wait()))
        raise
    waiter = asyncio.create_task(child.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=TIMEOUT)
        if not done:
            raise DrawingFailure('project_drawing_render_timeout')
        if waiter.result() != 0:
            error_file = destination / 'error.json'
            code = 'project_drawing_render_failed'
            if error_file.is_file() and error_file.stat().st_size <= 256:
                try:
                    candidate = json.loads(error_file.read_text())['code']
                    if isinstance(candidate, str) and re.fullmatch(r'project_drawing_[a-z_]{1,64}', candidate):
                        code = candidate
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            raise DrawingFailure(code)
    finally:
        if not waiter.done():
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Repeated cancellation cannot release the temporary directory or
            # platform operation lock while the renderer is still writing.
            await _join_without_recancel(waiter)


def _read_prepared(destination):
    path = destination / 'result.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise DrawingFailure('project_drawing_render_failed')
    try:
        result = json.loads(path.read_text())
        pages, total = result['pages'], result['total_pages']
        if (result['version'] != VERSION or not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES or
                type(total) is not int or not len(pages) <= total <= 1000 or
                result['truncated'] != (total > MAX_PAGES) or len(pages) != min(MAX_PAGES, total)):
            raise ValueError()
        prepared = []
        for i, page in enumerate(pages, 1):
            if (page['file'] != f'page-{i}.jpg' or page['page'] != i or page['media_type'] != 'image/jpeg' or
                    not isinstance(page.get('native_text'), str) or len(page['native_text']) > PAGE_TEXT_CHARS or
                    type(page.get('text_truncated')) is not bool or
                    type(page['bytes']) is not int or not 0 < page['bytes'] <= MAX_PAGE_BYTES or
                    any(type(page[k]) is not int or not 1 <= page[k] <= MAX_SIDE for k in ('width', 'height'))):
                raise ValueError()
            target = destination / page['file']
            if target.is_symlink() or not target.is_file() or target.stat().st_size != page['bytes']:
                raise ValueError()
            data = target.read_bytes()
            if not data.startswith(b'\xff\xd8') or hashlib.sha256(data).hexdigest() != page['sha256']:
                raise ValueError()
            prepared.append((page, data))
        if sum(len(data) for _, data in prepared) > MAX_OUTPUT:
            raise ValueError()
        return result, prepared
    except (OSError, KeyError, TypeError, ValueError):
        raise DrawingFailure('project_drawing_render_failed') from None


async def prepare_drawing(service, ctx, args):
    from .service import ProjectFailure, object_args
    object_args(args, {'attachment_id'}, {'attachment_id'})
    await service._authorize(ctx, 'heatcost')
    source = await service._attachment(ctx, args['attachment_id'], DRAWING_FORMATS, MAX_INPUT)
    suffix = Path(source['name']).suffix.lower()
    if not source['data']:
        raise ProjectFailure(400, 'project_drawing_input_empty')
    try:
        with tempfile.TemporaryDirectory(prefix='maimi-drawing-') as directory:
            base = Path(directory)
            input_path = base / ('source' + suffix)
            input_path.write_bytes(source['data'])
            await _render(input_path, suffix, base)
            result, prepared = _read_prepared(base)
            await service._authorize(ctx, 'heatcost')
            text_pages = [{'page': p['page'], 'text': p['native_text'], 'method': 'embedded-text' if p['native_text'] else 'unavailable',
                           'truncated': p['text_truncated']} for p, _ in prepared]
            text_warning = None
            if getattr(service, 'drawing_text', None) is not None:
                # Callback is context/selection bound in the gateway. It uses only
                # the already-installed shared Office service and fixed options.
                try:
                    if suffix in {'.webp', '.bmp', '.tif', '.tiff'}:
                        # Decode only in the bounded renderer. Existing Office OCR
                        # receives normalized single-page JPEGs, including TIFF pages.
                        normalized_text = []
                        for page, data in prepared:
                            await service._authorize(ctx, 'heatcost')
                            rows = await service.drawing_text(ctx, {'name': 'drawing.jpg', 'data': data}, 1)
                            if len(rows) != 1 or rows[0].get('page') != 1:
                                raise DrawingTextUnavailable('project_drawing_text_unavailable')
                            normalized_text.append({**rows[0], 'page': page['page']})
                        text_pages = normalized_text
                    else:
                        text_pages = await service.drawing_text(ctx, source, result['total_pages'])
                except DrawingTextUnavailable as exc:
                    text_warning = exc.code
            else:
                text_warning = 'project_drawing_text_unavailable'
            await service._authorize(ctx, 'heatcost')
            pages = []
            for page, data in prepared:
                await service._authorize(ctx, 'heatcost')
                artifact = await service.files.write(ctx, f'图纸识别-第{page["page"]}页.jpg', data, 'image/jpeg')
                pages.append({**{k: page[k] for k in ('page', 'media_type', 'width', 'height', 'bytes')},
                              'attachment_id': artifact['attachment_id']})
            await service._authorize(ctx, 'heatcost')
            return {'status': 'ready', 'drawing': {'version': VERSION, 'attachment_id': args['attachment_id'],
                    'pages': pages, 'total_pages': result['total_pages'], 'truncated': result['truncated'],
                    'text_pages': text_pages, 'text_warning': text_warning,
                    'warnings': (['本次只识别前三页；其余页未纳入成本依据，请拆分附件后分别分析。'] if result['truncated'] else []),
                    'units': {'length': 'mm', 'height': 'mm', 'width': 'mm', 'weight': 'g', 'finCount': 'pcs', 'holeCount': 'pcs'}}}
    except DrawingFailure as exc:
        raise ProjectFailure(400 if exc.code != 'project_drawing_renderer_missing' else 503, exc.code) from None


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--render' or sys.argv[3] not in DRAWING_FORMATS:
        raise SystemExit(2)
    destination = Path(sys.argv[4])
    try:
        _render_child(Path(sys.argv[2]), sys.argv[3], destination)
    except BaseException as exc:
        code = exc.code if isinstance(exc, DrawingFailure) else 'project_drawing_render_failed'
        try:
            (destination / 'error.json').write_text(json.dumps({'code': code}), encoding='utf-8')
        except OSError:
            pass
        raise SystemExit(1)
