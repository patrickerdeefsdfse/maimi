"""Generate company print badges from one validated workbook in one task."""
import asyncio
import io
import os
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment

from .artwork import CardPrintSizeError, CardValidationError, SinglePrintDocument, render_print_bytes
from .workbook import ImportValidationError, parse_workbook


@dataclass(frozen=True)
class BadgeResult:
    total: int
    printed: int
    pages: int
    issues: tuple[dict, ...]
    document: bytes | None


def template_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '员工名单'
    sheet.append(['工号', '姓名', '英文名', '照片'])
    sheet['B1'].comment = Comment('制作本人工卡可留空，按登录工号从公司目录补姓名。为他人制卡请填写姓名。', '麦米')
    sheet['C1'].comment = Comment('可选。填写后按原文印制，留空时按中文姓名生成拼音。特殊读音可在此指定。', '麦米')
    sheet.freeze_panes = 'A2'
    for letter, width in zip('ABCD', (20, 22, 32, 16)):
        sheet.column_dimensions[letter].width = width
    sheet.column_dimensions['A'].number_format = '@'
    output = io.BytesIO()
    try:
        workbook.save(output)
        return output.getvalue()
    finally:
        workbook.close()


async def generate_badges(workbook, fetch_photo, *, overrides=None, allow_partial=False,
                          check_access=None, progress=None, max_bytes=128*1024*1024, assets_dir=None):
    """Fetch only staff named by the workbook; keep the photos out of model results."""
    rows = await asyncio.to_thread(parse_workbook, workbook)
    return await generate_rows(rows, fetch_photo, overrides=overrides, allow_partial=allow_partial,
                               check_access=check_access, progress=progress, max_bytes=max_bytes, assets_dir=assets_dir)


async def generate_rows(rows, fetch_photo, *, overrides=None, allow_partial=False,
                        check_access=None, progress=None, max_bytes=128*1024*1024, assets_dir=None):
    """Internal entry for the bridge when it has already parsed and validated the workbook."""
    overrides = overrides or {}
    if not set(overrides) <= {row['employee_no'] for row in rows}:
        raise ImportValidationError([{'field':'补传照片','row':None,'message':'补传照片的工号必须在本次名单中。'}])
    if type(allow_partial) is not bool:
        raise ValueError('allow_partial must be boolean')
    assets = Path(assets_dir or os.environ.get('MAIMI_BADGE_ASSETS_DIR') or Path(__file__).with_name('assets'))
    if not (assets/'card-background.bmp').is_file() or not (assets/'fonts'/'SourceHanSansSC-Regular.otf').is_file():
        raise RuntimeError('badge_artwork_missing')

    async def photo(number):
        if number in overrides:
            return overrides[number]
        return await fetch_photo(number)

    async def update_document(function, *args, **kwargs):
        # A stopped chat must not close the ZIP while its background write is active.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    issues = []
    total = len(rows)
    writer = SinglePrintDocument(total, max_bytes)
    try:
        for start in range(0, total, 4):
            if check_access:
                await check_access()
            batch = rows[start:start+4]
            tasks = [asyncio.create_task(photo(row['employee_no'])) for row in batch]
            try:
                await asyncio.gather(*tasks)
                for idx, row in enumerate(batch):
                    data = tasks[idx].result()
                    reason = None
                    if data is None:
                        reason = '缺少照片，请按此工号补传。'
                    else:
                        try:
                            image = await asyncio.to_thread(render_print_bytes, row, data, assets)
                            await update_document(writer.add, image)
                        except CardValidationError as exc:
                            reason = str(exc)
                    if reason:
                        issues.append({'row':row['row'],'employee_no':row['employee_no'],'reason':reason})
                    if progress:
                        await progress(start+idx+1, total)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        if check_access:
            await check_access()
        if not writer.added or (issues and not allow_partial):
            return BadgeResult(total, 0, 0, tuple(issues), None)
        data = await update_document(writer.finish, allow_partial=allow_partial)
        return BadgeResult(total, writer.added, (writer.added+9)//10, tuple(issues), data)
    finally:
        writer.close()
