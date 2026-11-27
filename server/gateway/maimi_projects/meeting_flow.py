"""Meeting conversation orchestration; files/state stay behind platform ports."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
from uuid import uuid4
import zipfile

from .contracts import card, table, notice, invoke_action, text
from .meeting import KINDS, CATEGORIES, MAX_INPUT, MAX_DOCX, MAX_ARCHIVE, MeetingFailure, inspect_input, render_package
from .service import ProjectFailure, object_args, ref, CARD_MEDIA


def failure_card(job_id, code):
    messages = {
        'meeting_sheet_missing': 'Excel中缺少“三会议案清单”工作表，请检查后重新上传。',
        'meeting_input_invalid': '会议清单缺少有效的会议届次、日期或议案内容，请核对原表后重新上传。',
        'meeting_memo_title_invalid': '请在Excel的A1填写本次会议事项主题后重新上传。',
        'meeting_formula_values_required': '清单中有公式，请先将公式转为值后重新上传。',
        'meeting_sheet_limit': '会议清单行列数超出支持范围，请使用三会议案清单格式（最多100条议案）。',
        'meeting_archive_invalid': 'Excel文件格式无效或已损坏，请重新保存为.xlsx后上传。',
        'meeting_file_size_invalid': '会议Excel文件大小无效，单个输入不能超过25MiB。',
        'project_attachment_invalid': '请上传本条消息的.xlsx或.xlsm会议清单。',
        'meeting_archive_output_too_large': '会议文件包超过32MiB，当前未完成发布，请精简母版中的图片后重试。',
        'meeting_templates_not_configured': '公司会议母版尚未配置，请联系管理员。',
        'meeting_template_changed': '会议母版校验未通过，请联系管理员核对当前母版。',
        'meeting_cancelled': '会议文件生成已取消，未完成的文件包不会作为成功结果展示。',
    }
    return card('meeting', job_id, 'failed', messages.get(code, '会议文件未生成完成，请稍后重试；若持续失败，请联系管理员。'),
                sections=[notice('错误标识：' + code, 'warning')])


async def generate_from_workbook(service, ctx, args):
    """One deterministic operation under invoke's existing session lock.

    Input is an owner/session-authorized opaque reference. A completed job for
    the same input and exact template revision is reused. No model is involved.
    The platform separately deduplicates submission request IDs and fences jobs.
    """
    object_args(args, {'attachment_id'}, {'attachment_id'})
    attachment_id = ref(args['attachment_id'])
    # Give pre-parse failures a stable result identity without exposing a path.
    job_id = 'direct-' + hashlib.sha256(attachment_id.encode()).hexdigest()
    try:
        source = await service._attachment(ctx, attachment_id, {'.xlsx', '.xlsm'}, MAX_INPUT)
        if service.meeting_assets is None:
            raise ProjectFailure(503, 'meeting_templates_not_configured')
        source_hash = hashlib.sha256(source['data']).hexdigest()
        identity = json.dumps(['meeting-direct-v1', attachment_id, source_hash, service.meeting_assets.revision], separators=(',', ':'))
        job_id = 'direct-' + hashlib.sha256(identity.encode()).hexdigest()
        job = await service.state.get(ctx, 'meeting:' + job_id)
        if job is None:
            with tempfile.TemporaryDirectory(prefix='maimi-meeting-direct-') as directory:
                package, warnings = await cpu(inspect_input, source['data'], Path(directory) / 'meeting.xlsx')
            await service._authorize(ctx, 'meeting')
            job = {'id': job_id, 'attachment_id': attachment_id, 'sha256': source_hash,
                   'package': package.to_dict(), 'warnings': warnings, 'templates_configured': True,
                   'template_revision': service.meeting_assets.revision}
            job['package']['source_filename'] = source['name']
            await service.state.put(ctx, 'meeting:' + job_id, job)
        elif (not isinstance(job, dict) or job.get('attachment_id') != attachment_id or job.get('sha256') != source_hash or
              job.get('template_revision') != service.meeting_assets.revision):
            raise ProjectFailure(409, 'meeting_job_identity_invalid')
        if not job.get('documents'):
            await service._save_result(ctx, card('meeting', job_id, 'running', '正在按公司母版生成会议Word文件与完整ZIP包。'))
        # Do not call service.invoke here: its owner+session lock is already held.
        return await _invoke(service, ctx, 'generate', {'job_id': job_id})
    except (Exception, asyncio.CancelledError) as exc:
        code = 'meeting_cancelled' if isinstance(exc, asyncio.CancelledError) else getattr(exc, 'code', str(exc))
        # Only fixed error codes can be rendered. Never expose library trace,
        # file content, an endpoint or a credential as a result-card message.
        if not isinstance(code, str) or not code.startswith(('meeting_', 'project_')) or len(code) > 100 or not all(c.islower() or c == '_' for c in code):
            code = 'meeting_generation_failed'
        try:
            await service._save_result(ctx, failure_card(job_id, code))
        except (Exception, asyncio.CancelledError):
            # Revoked/cancelled platform permits may prevent even this write;
            # preserve the original error and let the platform mark job failure.
            pass
        raise


async def cpu(function, *args):
    # Do not remove a private temp directory while its CPU thread still writes.
    # No network/platform file publication occurs inside this worker thread.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A second UI/lease cancellation must not cancel the to_thread
                # future and allow its still-running thread to outlive temp.
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()  # Retrieve a worker failure without replacing cancellation.
        raise


def review_card(job, kind='board', offset=0):
    meetings = job['package']['meetings']
    if kind not in meetings:
        raise ProjectFailure(404, 'meeting_kind_not_in_workbook')
    current = meetings[kind]
    columns = lambda names: [{'key': key, 'label': label} for key, label in names]
    summary = table('会议清单', columns([('name', '会议'), ('notice', '通知日期'), ('date', '召开日期'), ('time', '时间'), ('count', '议案数')]),
                    [{'name': value['meeting_name'], 'notice': value['notice_date'], 'date': value['meeting_date'],
                      'time': value['meeting_time'], 'count': value['agenda_count']} for value in meetings.values()])
    agendas = current['agendas']
    detail = table(KINDS[kind] + ' · 议案核对', columns([('index', '原序号'), ('title', '议案'), ('children', '子议案'), ('recusal', '回避表决'), ('note', '原表标注')]),
                   [{'index': value['source_index'], 'title': value['title'], 'children': '；'.join(value['children']),
                     'recusal': value['recusal'], 'note': value['selection_note']} for value in agendas[offset:offset + 20]])
    detail.update(total_rows=len(agendas), offset=offset, truncated=offset + 20 < len(agendas))
    sections = [summary, detail, notice('议案介绍和无来源字段不自动编造；模板中的表决结论与保留字段需在正式使用前核对。', 'warning')]
    sections.extend(notice(value, 'warning') for value in job['warnings'][:7])
    actions = [invoke_action('查看' + KINDS[item], 'meeting', 'review', {'job_id': job['id'], 'kind': item}) for item in meetings if item != kind]
    for item in actions:
        item['id'] = 'review-' + item['arguments']['kind']
    if offset + 20 < len(agendas):
        actions.append(invoke_action('下一页议案', 'meeting', 'review', {'job_id': job['id'], 'kind': kind, 'offset': offset + 20}))
    if job.get('templates_configured'):
        actions.append(invoke_action('生成会议文件', 'meeting', 'generate', {'job_id': job['id']}))
    else:
        sections.append(notice('会议清单已解析；公司原始母版尚未接入，当前不能生成完整文件。', 'warning'))
    return card('meeting', job['id'], 'needs_input', '已解析' + str(len(meetings)) + '类会议，请核对日期、议案及回避表决。', sections=sections, actions=actions)


def documents_card(job, offset=0):
    documents = job.get('documents')
    if not documents or not job.get('archive'):
        raise ProjectFailure(409, 'meeting_documents_not_generated')
    # All generated Word files stay in the same authorized ZIP. The chat card
    # lists their names; it does not require pagination or duplicate 32 buttons.
    rows = [{'name': item['artifact']['name']} for item in documents]
    section = table('生成文件', [{'key': 'name', 'label': '文件名'}], rows)
    return card('meeting', job['id'], 'completed', f'已生成{len(documents)}份Word文件，已打包为ZIP。',
                sections=[section] + [notice(value, 'warning') for value in job.get('warnings', [])[:7]],
                artifacts=[job['archive']], actions=[])


async def invoke_meeting(service, ctx, action, args):
    try:
        return await _invoke(service, ctx, action, args)
    except MeetingFailure as exc:
        raise ProjectFailure(422, str(exc)) from None


async def _invoke(service, ctx, action, args):
    if action == 'generate_from_workbook':
        return await generate_from_workbook(service, ctx, args)
    if action == 'requirements':
        object_args(args, ())
        return await service._save_result(ctx, card('meeting', 'meeting-requirements', 'needs_input', '请随本会话上传董事会会议清单Excel。', sections=[
            notice('支持.xlsx/.xlsm，固定工作表“三会议案清单”：A1为备忘录主题，第2行通知日期、第3行召开日期和时分、第4行会议名称；A列议案序号、B列议案名称，从D列开始勾选各会议议案。'),
            notice('支持董事会、独立董事专门会议及战略、审计、提名、薪酬与考核委员会。股东会列不在原项目生成范围。最大100条议案；公式单元格请先转为值。'),
            notice('8份签字页，每类召开会议4份通知、表决办法及表决票、决议、会议记录；未召开委员会不生成。'),
            notice('公司母版已配置，生成时仍会逐个核验。' if service.meeting_assets else '公司原始母版尚未配置：可解析核对，不能生成完整文件。', 'info' if service.meeting_assets else 'warning')]))
    if action == 'import_workbook':
        object_args(args, {'attachment_id'}, {'attachment_id'})
        source = await service._attachment(ctx, args['attachment_id'], {'.xlsx', '.xlsm'}, MAX_INPUT)
        with tempfile.TemporaryDirectory(prefix='maimi-meeting-') as directory:
            package, warnings = await cpu(inspect_input, source['data'], Path(directory) / 'meeting.xlsx')
        await service._authorize(ctx, 'meeting')
        job = {'id': uuid4().hex, 'attachment_id': args['attachment_id'], 'sha256': hashlib.sha256(source['data']).hexdigest(),
               'package': package.to_dict(), 'warnings': warnings, 'templates_configured': service.meeting_assets is not None}
        job['package']['source_filename'] = source['name']
        await service.state.put(ctx, 'meeting:' + job['id'], job)
        return await service._save_result(ctx, review_card(job))
    required = {'job_id'}
    allowed = required | ({'kind', 'offset'} if action == 'review' else {'offset'} if action == 'documents' else set())
    object_args(args, allowed, required)
    job = await service.state.get(ctx, 'meeting:' + ref(args['job_id']))
    if not isinstance(job, dict):
        raise ProjectFailure(404, 'meeting_job_not_found')
    offset = args.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= (32 if action == 'documents' else 100):
        raise ProjectFailure(400, 'meeting_offset_invalid')
    if action == 'review':
        kind = args.get('kind', 'board')
        if not isinstance(kind, str) or kind not in KINDS:
            raise ProjectFailure(400, 'meeting_kind_invalid')
        job['templates_configured'] = service.meeting_assets is not None
        return await service._save_result(ctx, review_card(job, kind, offset))
    if action == 'documents':
        return await service._save_result(ctx, documents_card(job, offset))
    if service.meeting_assets is None:
        raise ProjectFailure(503, 'meeting_templates_not_configured')
    if job.get('documents'):
        return await service._save_result(ctx, documents_card(job))
    source = await service._attachment(ctx, job['attachment_id'], {'.xlsx', '.xlsm'}, MAX_INPUT)
    if hashlib.sha256(source['data']).hexdigest() != job['sha256']:
        raise ProjectFailure(409, 'meeting_input_changed')
    with tempfile.TemporaryDirectory(prefix='maimi-meeting-render-') as directory:
        package, warnings, rendered, packed = await cpu(render_package, source['data'], Path(directory), service.meeting_assets)
    await service._authorize(ctx, 'meeting')
    if job.get('published_documents') and job.get('template_revision') != service.meeting_assets.revision:
        raise ProjectFailure(409, 'meeting_template_revision_changed')
    job['template_revision'] = service.meeting_assets.revision
    published = list(job.get('published_documents', []))
    # Known successful publications are journalled so a retry after a later
    # write failure resumes instead of creating every document a second time.
    if len(published) > len(rendered) or any(
        saved.get('meeting_kind') != item['meeting_kind'] or saved.get('category') != item['category'] or
        saved.get('artifact', {}).get('name') != item['name'] for saved, item in zip(published, rendered)
    ):
        raise ProjectFailure(409, 'meeting_publication_state_invalid')
    if published:
        # DOCX/ZIP metadata timestamps may differ on a later retry. Preserve
        # exact bytes already published so the ZIP and single-file links agree.
        for saved, item in zip(published, rendered):
            previous = await service._attachment(ctx, saved['artifact']['attachment_id'], {'.docx'}, MAX_DOCX)
            if previous['name'] != item['name'] or hashlib.sha256(previous['data']).hexdigest() != saved.get('sha256'):
                raise ProjectFailure(409, 'meeting_published_file_changed')
            item['data'] = previous['data']
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for item in rendered:
                output.writestr(item['filename'], item['data'])
        packed = archive_buffer.getvalue()
        if len(packed) > MAX_ARCHIVE:
            raise ProjectFailure(413, 'meeting_archive_output_too_large')
    for item in rendered[len(published):]:
        await service._authorize(ctx, 'meeting')
        artifact = await service.files.write(ctx, item['name'], item['data'], CARD_MEDIA)
        published.append({'meeting_kind': item['meeting_kind'], 'category': item['category'], 'artifact': artifact,
                          'sha256': hashlib.sha256(item['data']).hexdigest()})
        job['published_documents'] = list(published)
        await service.state.put(ctx, 'meeting:' + job['id'], job)
    await service._authorize(ctx, 'meeting')
    archive = await service.files.write(ctx, '董事会会议文件包.zip', packed, 'application/zip')
    await service._authorize(ctx, 'meeting')
    job.update(documents=published, archive=archive, template_revision=service.meeting_assets.revision)
    job.pop('published_documents', None)
    await service.state.put(ctx, 'meeting:' + job['id'], job)
    return await service._save_result(ctx, documents_card(job))
