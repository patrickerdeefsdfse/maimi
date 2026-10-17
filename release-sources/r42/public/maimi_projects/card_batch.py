"""One lossless print document from source photos, never stored previews."""
from __future__ import annotations

import hashlib
import json
from .card_renderer import PRINT_LAYOUT, CardValidationError, CardPrintSizeError, SinglePrintDocument, render_print_bytes
from .meeting_flow import cpu
from .service import CARD_MEDIA, ProjectFailure

MAX_PRINT_FILE = 128 * 1024 * 1024
PRINT_FORMAT_VERSION = 3
PER_PAGE = 10


async def generate_print(service, ctx, job, selected, layout):
    """All selected employees or no output; normal preview damage is irrelevant.

    The fixed print profile preserves 1673 x 1063 pixels per card, 85 x 54 mm,
    ten cards per A4 page, original employee order and unmodified QR values.
    The caller/file port still enforces owner/session/fence/quota boundaries.
    """
    identity = {'format_version': PRINT_FORMAT_VERSION, 'layout': {**PRINT_LAYOUT, **layout},
                'employees': [(e['id'], e.get('employee_no'), e.get('photo'), e.get('name'), e.get('english_name')) for e in selected]}
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if job.get('print_signature') == signature and job.get('print_format_version') == PRINT_FORMAT_VERSION and len(job.get('documents', [])) == 1:
        await service._authorize(ctx, 'employee-cards')
        await service._card_progress('cards_publish', 1, 1)
        return True
    writer = SinglePrintDocument(len(selected), MAX_PRINT_FILE, **layout)
    try:
        errors = 0
        await service._card_progress('cards_prepare', 0, len(selected))
        for index, employee in enumerate(selected):
            await service._authorize(ctx, 'employee-cards')
            # A previous preview may be missing, rotated, truncated or stale.
            # Its opaque reference is deliberately never dereferenced here.
            try:
                photo = await service._attachment(ctx, employee['photo']['attachment_id'], {'.png'}, 10 * 1024 * 1024)
            except ProjectFailure as error:
                if error.status not in {400, 404} and error.code != 'project_attachment_integrity_failed':
                    raise
                employee.pop('photo', None)
                employee.pop('preview', None)
                employee.update(status='missing', error='源照片已失效，请重试取照或补传照片。')
                errors += 1
                await service._card_progress('cards_prepare', index + 1, len(selected))
                continue
            try:
                artwork = await cpu(render_print_bytes, employee, photo['data'], service.assets_dir)
                await cpu(writer.add, artwork)
                del artwork
            except CardValidationError as error:
                employee.update(status='invalid', error=str(error)[:300])
                errors += 1
            except CardPrintSizeError:
                raise ProjectFailure(413, 'project_card_single_file_too_large') from None
            except OSError:
                raise ProjectFailure(500, 'project_card_document_invalid') from None
            else:
                employee.pop('preview', None)
                employee.update(status='ready', error=None)
            await service._card_progress('cards_prepare', index + 1, len(selected))
        if errors:
            # Never emit a partial batch or show the old document as the new one.
            job.pop('document', None)
            job.pop('documents', None)
            job.pop('archive', None)
            job['edit_error'] = f'{errors} 人的源照片或工卡信息需要处理，整批尚未生成；请按名单提示修正后重试。'
            await service.state.put(ctx, 'cards:' + job['id'], job)
            return False
        total_pages = (len(selected) + PER_PAGE - 1) // PER_PAGE
        await service._card_progress('cards_document', 0, total_pages)
        try:
            content = await cpu(writer.finish)
        except CardValidationError:
            raise ProjectFailure(500, 'project_card_document_invalid') from None
        except CardPrintSizeError:
            raise ProjectFailure(413, 'project_card_single_file_too_large')
        except OSError:
            raise ProjectFailure(500, 'project_card_document_invalid') from None
        await service._card_progress('cards_document', total_pages, total_pages)
        await service._authorize(ctx, 'employee-cards')
        name = '员工工卡.docx'
        content_key = hashlib.sha256(name.encode())
        content_key.update(b'\x00')
        content_key.update(content)
        key = content_key.hexdigest()
        pending = job.get('print_pending', {})
        saved = pending.get('parts', {}) if pending.get('signature') == signature else {}
        artifact = saved.get(key)
        await service._card_progress('cards_publish', 0, 1)
        if not artifact:
            # Dedicated trusted operation; generic uploaded/generated artifacts
            # retain their original limit. The file port owns policy checks.
            artifact = await service.files.write_card_print(ctx, name, content, CARD_MEDIA)
            job['print_pending'] = {'signature': signature, 'parts': {key: artifact}}
            await service.state.put(ctx, 'cards:' + job['id'], job)
        job.update(documents=[artifact], document=artifact, print_signature=signature,
                   print_format_version=PRINT_FORMAT_VERSION,
                   layout={'pages': total_pages, 'columns': 2, 'rows': 5, 'count': len(selected)})
        job.pop('archive', None)
        job.pop('edit_error', None)
        job.pop('print_pending', None)
        # Persist before progress so a disconnected UI cannot orphan a file.
        await service.state.put(ctx, 'cards:' + job['id'], job)
        await service._card_progress('cards_publish', 1, 1)
        return True
    finally:
        writer.close()
