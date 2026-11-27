"""Owner-scoped DSH actions for templates and a single printable badge batch."""
import asyncio
import io
import re
import unicodedata
from uuid import uuid4

from .contracts import card, table
from .service import ProjectFailure, object_args
from .badge_photos import fetch_photo, fetch_self_details, PhotoServiceError


KEY = 'employee-badges'
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
FILE_ID = re.compile(r'pjf_[A-Za-z0-9_-]{1,100}\Z')
ISSUE_COLUMNS = [('row','行号'), ('employee_no','工号'), ('field','字段'), ('reason','原因')]
# Each API replica serves login as well as tools. Queue jobs before retaining
# any workbook, decoded image or DOCX bytes. The runtime supplies cancellation
# and a hard execution deadline to waiting as well as running jobs.
_PRINT_SLOT = asyncio.Semaphore(1)


def _printable_employee_name(value):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if (not value or len(value) > 24 or re.fullmatch(r'(?:M[0-9]{8}|[0-9]+)', value, re.I)
            or any(unicodedata.category(char) in {'Cc','Cf','Cs','Zl','Zp'} for char in value)):
        return ''
    return value


def _complete_badge_names(rows, actor_number, actor=None):
    """Only the authenticated actor can use the directory for a missing name."""
    from app.iam_directory import DirectoryError, check_snapshot
    from .employee_badges.workbook import complete_print_name

    needs_self_name = any(row['employee_no'] == actor_number and not row['name'] for row in rows)
    own_name = ''
    if getattr(actor, 'user_no', None) == actor_number:
        own_name = _printable_employee_name(getattr(actor, 'display_name', ''))
        if own_name.casefold() == str(getattr(actor, 'account_no', '')).strip().casefold():
            own_name = ''
    directory_unavailable = False
    if needs_self_name and not own_name:
        try:
            snapshot = check_snapshot()
        except DirectoryError:
            directory_unavailable = True
        else:
            matches = set()
            for account in snapshot['accounts']:
                if account['active'] and account['user_no'] == actor_number:
                    matches.add((account.get('display_name') or '').strip())
            if len(matches) == 1:
                own_name = _printable_employee_name(matches.pop())

    issues = []
    for row in rows:
        number = row['employee_no']
        if not row['name']:
            if number != actor_number:
                issues.append({'row': row['row'], 'employee_no': number, 'field': '姓名',
                               'reason': '为其他员工制卡时，请在名单中填写该员工的姓名。'})
                continue
            if not own_name:
                reason = ('公司员工目录暂不可用，请稍后重试或在名单中补填姓名。'
                          if directory_unavailable else '未能按工号确认姓名，请在名单中补填姓名。')
                issues.append({'row': row['row'], 'employee_no': number, 'field': '姓名', 'reason': reason})
                continue
            row['name'] = own_name
        if not row['english_name']:
            try:
                complete_print_name(row)
            except ValueError:
                issues.append({'row': row['row'], 'employee_no': number, 'field': '英文名',
                               'reason': '未能生成可印刷的拼音，请在名单中填写英文名或拼音。'})
    return issues


def _issue_workbook(issues):
    from openpyxl import Workbook
    workbook = Workbook()
    try:
        sheet = workbook.active
        sheet.title = '待处理'
        sheet.append([label for _, label in ISSUE_COLUMNS])
        for row in issues:
            sheet.append([row.get(key) for key,_ in ISSUE_COLUMNS])
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value,str):
                    cell.data_type='s'
        sheet.freeze_panes = 'A2'
        for column,width in zip('ABCD', (12,18,18,65)):
            sheet.column_dimensions[column].width=width
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()
    finally:
        workbook.close()


async def invoke_badges(service, ctx, action, arguments):
    if action == 'generate':
        async with _PRINT_SLOT:
            return await _invoke(service,ctx,action,arguments)
    return await _invoke(service,ctx,action,arguments)


async def _invoke(service, ctx, action, arguments):
    from .employee_badges import generate_rows, parse_workbook, template_bytes, CardPrintSizeError, ImportValidationError
    async def check_access():
        await service._authorize(ctx, KEY)

    async def attachment(file_id, suffixes, maximum):
        await check_access()
        if not isinstance(file_id,str) or not FILE_ID.fullmatch(file_id):
            raise ProjectFailure(400, 'project_attachment_invalid')
        _, source = await service.files.record(file_id)
        if source != 'native_message':
            raise ProjectFailure(403, 'project_attachment_invalid')
        return (await service._attachment(ctx,file_id,suffixes,maximum))['data']

    async def save(status, summary, *, issues=(), artifacts=()):
        sections = []
        artifacts = list(artifacts)
        if issues:
            columns = [dict(key=key,label=label) for key,label in ISSUE_COLUMNS]
            sections.append(table('需要处理的名单行',columns,issues,maximum=24))
            if len(issues)>24:
                report = await asyncio.to_thread(_issue_workbook,issues)
                await check_access()
                artifacts.append(await service.files.write(ctx,'工卡待处理清单.xlsx',report,XLSX))
        return await service._save_result(ctx,card(KEY,'badges-'+uuid4().hex,status,summary,
                                                   sections=sections,artifacts=artifacts))

    if action == 'template':
        object_args(arguments,set())
        content = await asyncio.to_thread(template_bytes)
        await check_access()
        artifact = await service.files.write(ctx,'工卡输入模板.xlsx',content,XLSX)
        return await save('completed','工卡模板已生成。制作本人工卡只需填工号，制作他人工卡请同时填写姓名。可指定英文名；本人资料未提供英文名时采用姓名拼音。',artifacts=[artifact])
    if action != 'generate':
        raise ProjectFailure(404,'project_action_not_found')
    object_args(arguments,{'attachment_id','photo_overrides','allow_partial'},{'attachment_id'})
    override_refs = arguments.get('photo_overrides',[])
    allow_partial = arguments.get('allow_partial',False)
    if not isinstance(override_refs,list) or len(override_refs)>20 or type(allow_partial) is not bool:
        raise ProjectFailure(400,'project_arguments_invalid')
    workbook = await attachment(arguments['attachment_id'],{'.xlsx'},10*1024*1024)
    try:
        rows = await asyncio.to_thread(parse_workbook,workbook,allow_missing_names=True)
    except ImportValidationError as exc:
        issues=[{**row,'reason':row['message']} for row in exc.errors]
        return await save('needs_input','请按行修正名单后重新上传。',issues=issues)
    employee_numbers={r['employee_no'] for r in rows}
    references={}
    for override in override_refs:
        object_args(override,{'employee_no','attachment_id'},{'employee_no','attachment_id'})
        number, file_id = override['employee_no'], override['attachment_id']
        if (not isinstance(number,str) or number not in employee_numbers or number in references
            or not isinstance(file_id,str) or not FILE_ID.fullmatch(file_id) or file_id in references.values()):
            raise ProjectFailure(400,'project_arguments_invalid')
        references[number]=file_id
    overrides={}
    remaining=32*1024*1024
    for employee,file_id in references.items():
        data=await attachment(file_id,{'.jpg','.jpeg','.png','.webp','.bmp'},min(remaining,8*1024*1024))
        remaining-=len(data)
        overrides[employee]=data
    await check_access()
    own_row = next((row for row in rows if row['employee_no'] == ctx.user_no and not row['name']), None)
    lookup_error, photo_error = '', ''
    if own_row is not None:
        # Reuse this single HR read for the print stage, even when the photo
        # is absent. An explicitly uploaded photo always wins.
        need_photo = ctx.user_no not in overrides
        profile = None
        try:
            profile = await fetch_self_details(ctx.user_no, include_photo=need_photo)
        except PhotoServiceError as exc:
            lookup_error = str(exc)
        await check_access()
        if profile is not None:
            own_row['name'] = _printable_employee_name(profile.name)
            if not own_row['english_name']:
                own_row['english_name'] = profile.english_name
        if need_photo:
            overrides[ctx.user_no] = profile.photo if profile else None
            photo_error = profile.photo_error if profile else lookup_error
    actor = getattr(getattr(service.files, 'access', None), 'actor', None)
    name_issues = await asyncio.to_thread(_complete_badge_names,rows,ctx.user_no,actor)
    await check_access()
    if name_issues:
        for issue in name_issues:
            if issue['employee_no'] == ctx.user_no and issue['field'] == '姓名':
                issue['reason'] = (lookup_error or '人员资料未能提供本人工卡姓名。') + '请核对工号，或在名单中补填姓名。'
        return await save('needs_input','请按下列行补填姓名或英文名后重新上传。',issues=name_issues)
    async def progress(done,total):
        if service.progress:
            await service.progress('cards_document',done,total)
    try:
        result = await generate_rows(rows,fetch_photo,overrides=overrides,allow_partial=allow_partial,
                check_access=check_access,progress=progress,assets_dir='/opt/maimi/r93-assets/cards')
    except PhotoServiceError as exc:
        return await save('failed',str(exc))
    except CardPrintSizeError:
        return await save('needs_input','工卡文件超过单次容量，请将名单拆成较小的批次上传。')
    except RuntimeError as exc:
        if str(exc)!='badge_artwork_missing':
            raise
        return await save('failed','工卡底图或印刷字体缺失，请联系管理员。')
    issues = [dict(issue, reason=photo_error) if photo_error and issue['employee_no'] == ctx.user_no
              else issue for issue in result.issues]
    if result.document is None:
        return await save('needs_input',f'名单共 {result.total} 人，{len(issues)} 人的照片需要处理。请按工号补传；也可明确选择只制作资料齐全的员工。',issues=issues)
    await check_access()
    artifact = await service.files.write_card_print(ctx,'员工工卡.docx',result.document,DOCX)
    summary=f'已生成 {result.printed} 张工卡，共 {result.pages} 页。'
    if issues:
        summary += f' 本次跳过 {len(issues)} 人。'
    return await save('completed',summary,issues=issues,artifacts=[artifact])
