"""Business adapters for selected DSH conversation tools.

The gateway supplies a server-authenticated Context and reauthorization hook.
No identity, location, output path, endpoint or credential is accepted from a
model argument. ``state`` is an encrypted, owner+session scoped store with a
distributed lock; ``files`` is the existing DSH attachment/workspace boundary.
Both ports must enforce the context again on every read/write. No independent
login, chat, upload store, AI key or browser application is introduced here.
"""
from __future__ import annotations

import asyncio
import copy
import io
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .contracts import NAMES, KEYS, OPAQUE, card, table, notice, invoke_action, text, cell
from .heatcost import calculate, FIELDS, PROCESS_RATES
from .monitor import LOCATIONS, MonitorFailure
from .monitor_projection import DISK_SECTIONS, project_disk

VERSION = '1.0.1'
MONITOR_USERS = frozenset({'M00003307', 'M00001069', 'M00096389'})
CARD_MEDIA = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
CARD_LAYOUT = {'card_width_mm': (85, 85, 85), 'card_height_mm': (54, 54, 54),
               'gap_mm': (1.5, 1.5, 1.5), 'page_margin_mm': (10, 10, 10)}


class ProjectFailure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass(frozen=True)
class Context:
    user_no: str
    native_session_id: str
    location: str
    selected: tuple[str, ...]
    # Must be taken from the authenticated host/lease, not a browser parameter.
    client_key: str
    # Trusted gateway permit source; never copied from tool arguments.
    source: str = 'model'


def object_args(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ProjectFailure(400, 'project_arguments_invalid')
    return value


def ref(value):
    if not isinstance(value, str) or not OPAQUE.fullmatch(value):
        raise ProjectFailure(400, 'project_reference_invalid')
    return value


def schema(properties=None, required=()):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties or {}, 'required': list(required)}


REF_SCHEMA = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,128}$'}
TOOLS = {
    'meeting': [
        ('generate_from_workbook', '直接将本条消息的三会议案清单Excel按公司母版生成Word与ZIP；解析、校验和生成一次完成，不需模型补写或额外确认', schema({'attachment_id': REF_SCHEMA}, ('attachment_id',))),
        ('requirements', '查看董事会会议文件的Excel格式、母版状态和输出类型；不是通用会议纪要', schema()),
        ('import_workbook', '解析本会话的三会议案清单Excel，核对会议日期、议案与回避表决；不代填无来源字段', schema({'attachment_id': REF_SCHEMA}, ('attachment_id',))),
        ('review', '分页核对本会话已导入会议及议案', schema({'job_id': REF_SCHEMA, 'kind': {'enum': ['board', 'independent_directors', 'strategy_committee', 'audit_committee', 'nomination_committee', 'compensation_committee']}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100}}, ('job_id',))),
        ('generate', '按已导入清单和已配置公司母版生成签字页、通知、表决票、决议、记录；用户需核对原表和模板保留字段', schema({'job_id': REF_SCHEMA}, ('job_id',))),
        ('documents', '分页查看本会话已生成的Word及完整ZIP包', schema({'job_id': REF_SCHEMA, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 32}}, ('job_id',))),
    ],
    'employee-cards': [
        ('template', '生成空白工卡Excel模板，保存在当前工作区', schema()),
        ('import_workbook', '读取本会话工卡名单、自动取照并直接生成一份打印Word；缺照或信息有误时返回待处理名单', schema({'attachment_id': REF_SCHEMA}, ('attachment_id',))),
        ('review', '分页查看已导入工卡名单和缺照状态', schema({'job_id': REF_SCHEMA, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 500}}, ('job_id',))),
        ('edit_employee', '修改工卡姓名/英文名、选择状态或用本会话图片补照；工号不可改', schema({
            'job_id': REF_SCHEMA, 'employee_id': REF_SCHEMA, 'name': {'type': 'string', 'maxLength': 24},
            'english_name': {'type': 'string', 'maxLength': 64}, 'selected': {'type': 'boolean'},
            'photo_attachment_id': REF_SCHEMA}, ('job_id', 'employee_id'))),
        ('retry_photos', '只重新取当前批次缺失的照片', schema({'job_id': REF_SCHEMA}, ('job_id',))),
        ('generate', '从源照片和当前名单直接生成一份A4打印Word，每页10张、每张8.5×5.4厘米，无预览文件', schema({
            'job_id': REF_SCHEMA, 'layout': schema({k: {'type': 'number', 'minimum': v[0], 'maximum': v[1]}
             for k, v in CARD_LAYOUT.items()})}, ('job_id',))),
    ],
    'infrastructure-monitor': [
        ('status', '查看当前集群连接状态；未连接会在当前会话显示安全登录表单', schema()),
        ('refresh', '读取本人当前已连接集群的性能或磁盘健康；不会修改vCenter', schema({'kind': {'enum': ['performance', 'disk']}})),
        ('view', '分页查看本会话最近一次采集结果，不重新登录或重复取数', schema({'kind': {'enum': ['performance', 'disk']},
         'section': {'enum': ['host_resources', 'vm_resources', 'storage_resources', 'alert_resources'] + [key for key, _, _ in DISK_SECTIONS]},
         'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000}}, ('kind', 'section', 'offset'))),
        ('disconnect', '清除本人当前会话的集群连接凭证', schema()),
    ],
    'heatcost': [
        ('review', '查看本会话最近的图纸成本结果及人工修改入口，不重新识别旧图纸', schema({'analysis_id': REF_SCHEMA})),
        ('prepare_drawing', '读取本条消息的图纸附件并准备受控字段识别；不由模型选择是否启动，缺失值不得编造', schema({'attachment_id': REF_SCHEMA}, ('attachment_id',))),
        ('drawing_requirements', '返回图纸识别字段规范；由当前会话模型读取本会话图纸，禁止猜测缺失数值', schema()),
        ('calculate', '依据本会话图纸的已识别字段确定性计算参考成本；模型不得编造图纸值或金额', schema({
            'attachment_id': REF_SCHEMA,
            'fields': {'type': 'object', 'description': '字段key见drawing_requirements。值为{value,evidence,confidence}；未识别value=null。'},
            'processes': {'type': 'object', 'description': '工艺key见drawing_requirements；值为{enabled,billable,evidence}。不确定的工艺省略。'},
            'rates': {'type': 'object', 'description': '只填用户明确提供的单价；不得由模型自行报价。'},
            'scrap_weight_g': {'type': 'number', 'minimum': 0, 'maximum': 1000000}}, ('attachment_id', 'fields', 'processes'))),
    ],
}


class ProjectSkillService:
    def __init__(self, *, authorize, state, files, assets_dir=None, photo_client=None, monitor=None, clock=time.time,
                 meeting_assets_dir=None, meeting_assets_manifest=None, drawing_text=None, progress=None):
        self.authorize, self.state, self.files = authorize, state, files
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self.photo_client, self.monitor, self.clock = photo_client, monitor, clock
        self.drawing_text = drawing_text
        self.progress = progress
        self.meeting_assets = None
        if meeting_assets_dir is not None or meeting_assets_manifest is not None:
            from .meeting import MeetingAssets
            if meeting_assets_dir is None or meeting_assets_manifest is None:
                raise ValueError('meeting_assets_configuration_incomplete')
            self.meeting_assets = MeetingAssets(meeting_assets_dir, meeting_assets_manifest)

    async def _authorize(self, ctx, key=None, selected=True):
        if (not isinstance(ctx, Context) or not re.fullmatch(r'M[0-9]{8}', ctx.user_no) or
                ctx.location not in {'cloud', 'local'} or not ctx.client_key):
            raise ProjectFailure(403, 'project_identity_invalid')
        try:
            UUID(ctx.native_session_id.removeprefix('session-'))
        except (ValueError, TypeError):
            raise ProjectFailure(400, 'project_session_invalid') from None
        await self.authorize(ctx)
        if key and key not in KEYS:
            raise ProjectFailure(404, 'project_not_found')
        if key == 'infrastructure-monitor' and (ctx.user_no not in MONITOR_USERS or ctx.location != 'cloud'):
            raise ProjectFailure(403, 'project_not_authorized')
        if selected and key and key not in ctx.selected:
            raise ProjectFailure(403, 'project_not_selected')

    async def catalog(self, ctx):
        await self._authorize(ctx, selected=False)
        items = []
        for key in KEYS:
            if key == 'infrastructure-monitor' and (ctx.user_no not in MONITOR_USERS or ctx.location != 'cloud'):
                continue
            status, reason = 'ready', ''
            if key == 'meeting' and self.meeting_assets is None:
                status, reason = 'limited', 'meeting_templates_not_configured'
            elif key == 'infrastructure-monitor' and self.monitor is None:
                status, reason = 'unavailable', 'monitor_service_not_configured'
            elif key == 'employee-cards' and self.assets_dir is None:
                status, reason = 'unavailable', 'card_assets_not_configured'
            elif key == 'employee-cards' and self.photo_client is None:
                status, reason = 'limited', 'photo_service_not_configured'
            descriptions = {'meeting': '核对董事会会议清单，按公司母版生成签字页及成套Word', 'employee-cards': '导入名单、核对照片并生成打印工卡',
                            'infrastructure-monitor': '在对话中查看集群性能和磁盘健康', 'heatcost': '从图纸提取字段并核算参考成本'}
            items.append({'plugin_key': key, 'version': VERSION, 'name': NAMES[key], 'description': descriptions[key],
                          'status': status, 'reason': reason, 'connect_url': None, 'authorization_url': None,
                          'tools': [{'name': action, 'description': description, 'input_schema': shape,
                                     'requires_confirmation': False, 'available': status == 'ready' or (key == 'meeting' and action not in {'generate', 'generate_from_workbook'})
                                     or (key == 'employee-cards' and status == 'limited' and action != 'retry_photos')}
                                    for action, description, shape in TOOLS[key]]})
        return items

    async def _save_result(self, ctx, result):
        await self._authorize(ctx, result['connector'])
        existing = await self.state.get(ctx, 'results') or []
        existing = [r for r in existing if r['id'] != result['id']]
        await self.state.put(ctx, 'results', (existing + [result])[-12:])
        return {'status': result['status'], 'project_result': result}

    async def results(self, ctx):
        await self._authorize(ctx, selected=False)
        results = await self.state.get(ctx, 'results') or []
        return [r for r in results if r['connector'] != 'infrastructure-monitor' or
                (ctx.user_no in MONITOR_USERS and ctx.location == 'cloud')]

    async def invoke(self, ctx, connector, action, arguments):
        await self._authorize(ctx, connector)
        if not any(item[0] == action for item in TOOLS[connector]) and not (connector == 'heatcost' and action == 'revise' and ctx.source == 'human'):
            raise ProjectFailure(404, 'project_action_not_found')
        async with self.state.lock(ctx):
            await self._authorize(ctx, connector)
            if connector == 'employee-cards':
                return await self._cards(ctx, action, arguments)
            if connector == 'heatcost':
                return await self._heatcost(ctx, action, arguments)
            if connector == 'infrastructure-monitor':
                return await self._monitor(ctx, action, arguments)
            if connector == 'meeting':
                from .meeting_flow import invoke_meeting
                return await invoke_meeting(self, ctx, action, arguments)
        raise ProjectFailure(503, 'project_source_review_pending')

    async def _attachment(self, ctx, attachment_id, suffixes, maximum):
        # Files port must resolve and authorize an opaque reference, never a
        # filesystem path or arbitrary URL. Shared upload path remains HTTPS443.
        value = await self.files.read(ctx, ref(attachment_id), maximum)
        if (not isinstance(value, dict) or not isinstance(value.get('name'), str) or
                not isinstance(value.get('data'), bytes) or len(value['data']) > maximum or
                Path(value['name']).suffix.lower() not in suffixes):
            raise ProjectFailure(400, 'project_attachment_invalid')
        return value

    async def _job(self, ctx, job_id):
        value = await self.state.get(ctx, 'cards:' + ref(job_id))
        if not value:
            raise ProjectFailure(404, 'project_job_not_found')
        return value

    async def _card_progress(self, phase, completed, total):
        if self.progress is not None:
            await self.progress(phase, completed, total)

    async def _card_validate(self, ctx, employee, photo_data):
        from .card_renderer import validate_card
        from .meeting_flow import cpu
        await cpu(validate_card, employee, photo_data, self.assets_dir)
        await self._authorize(ctx, 'employee-cards')
        employee.pop('preview', None)
        employee.update(status='ready', error=None)

    async def _card_auto_print(self, ctx, job):
        selected = [e for e in job['employees'] if e['selected']]
        if selected and all(e.get('photo') and e.get('status') == 'ready' for e in selected):
            from .card_batch import generate_print
            await generate_print(self, ctx, job, selected, {})

    async def _fetch_photos(self, ctx, job):
        job['photo_service_configured'] = self.photo_client is not None
        if self.photo_client is None:
            for employee in job['employees']:
                if not employee.get('photo'):
                    employee.update(status='missing', error='自动取照服务尚未配置，可手动补传照片。')
            return
        limit = asyncio.Semaphore(4)
        completed = sum(bool(e.get('photo')) for e in job['employees'])
        await self._card_progress('cards_photos', completed, len(job['employees']))
        async def fetch(employee):
            nonlocal completed
            if employee.get('photo'):
                return
            async with limit:
                # Authorization can expire while this entry waits behind the
                # four concurrent fetches. Recheck before sending a new request.
                await self._authorize(ctx, 'employee-cards')
                try:
                    from .card_photos import PhotoError
                    from .card_renderer import CardValidationError
                    if employee.get('photo'):
                        data = (await self._attachment(ctx, employee['photo']['attachment_id'], {'.png'}, 10 * 1024 * 1024))['data']
                    else:
                        data = await asyncio.to_thread(self.photo_client.fetch, employee['employee_no'])
                        await self._authorize(ctx, 'employee-cards')
                        employee['photo'] = await self.files.write(ctx, employee['employee_no'] + '-照片.png', data, 'image/png')
                    employee.pop('preview', None)
                    employee.update(status='ready', error=None)
                except PhotoError:
                    employee.update(status='missing', error='未取得可用照片，请重试或手动补传。')
                except CardValidationError as exc:
                    employee.pop('preview', None)
                    employee.update(status='invalid', error=text(exc, 300))
                finally:
                    completed += 1
                    await self._card_progress('cards_photos', completed, len(job['employees']))
        tasks = [asyncio.create_task(fetch(employee)) for employee in job['employees']]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # gather() alone leaves siblings running after the first failure.
            # Stop queued work and join all coroutines before returning. A fetch
            # already executing in a thread may finish, but no later file write
            # or new provider request is dispatched by its cancelled coroutine.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _card_result(self, ctx, job, offset=0):
        rows = job['employees'][offset:offset + 30]
        ready = sum(bool(e.get('photo')) and e.get('status') == 'ready' for e in job['employees'])
        columns = [{'key': k, 'label': label} for k, label in
                   [('id', '记录'), ('employee_no', '工号'), ('name', '姓名'), ('english_name', '英文名'), ('status', '工卡状态'), ('error', '需处理'), ('selected', '选中')]]
        section = table('名单核对', columns, rows)
        section.update(total_rows=len(job['employees']), offset=offset, truncated=offset + 30 < len(job['employees']))
        photo_options = []
        if callable(getattr(self.files, 'list', None)):
            # The files port is already bound to the authenticated owner and
            # native session. Never derive a file ref from a path or a name.
            inventory = await self.files.list()
            photo_options = [{'value': value['attachment_id'], 'label': text(value['name'], 160)}
                             for value in inventory if isinstance(value, dict)
                             and isinstance(value.get('attachment_id'), str) and OPAQUE.fullmatch(value['attachment_id'])
                             and isinstance(value.get('name'), str)
                             and Path(value['name']).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}][-100:]
        actions = [{'id': 'card-edit', 'label': '修改信息或补照', 'kind': 'project_form', 'form': 'card-edit',
                    'connector': 'employee-cards', 'action': 'edit_employee', 'arguments': {'job_id': job['id']},
                    'fields': [
                        {'name': 'employee_id', 'label': '本页员工', 'type': 'select', 'required': True,
                         'options': [{'value': e['id'], 'label': text(e['employee_no'] + ' · ' + e['name'], 100)} for e in rows]},
                        {'name': 'name', 'label': '姓名（留空不修改）', 'type': 'text', 'maxLength': 24},
                        {'name': 'english_name', 'label': '英文名（留空不修改）', 'type': 'text', 'maxLength': 64},
                        {'name': 'selected', 'label': '是否制作', 'type': 'select', 'value_type': 'optional_boolean',
                         'options': [{'value': '', 'label': '保持当前选择'}, {'value': 'true', 'label': '制作'}, {'value': 'false', 'label': '不制作'}]},
                        {'name': 'photo_attachment_id', 'label': '替换照片（先附加图片发送，再在这里选择）', 'type': 'select',
                         'options': [{'value': '', 'label': '不替换照片'}] + photo_options}]}]
        actions += ([invoke_action('重试缺照', 'employee-cards', 'retry_photos', {'job_id': job['id']})] if self.photo_client else []) + [
                   invoke_action('生成打印文件', 'employee-cards', 'generate', {'job_id': job['id']})]
        if offset + 30 < len(job['employees']):
            actions.append(invoke_action('下一页', 'employee-cards', 'review', {'job_id': job['id'], 'offset': offset + 30}))
        sections = [section, notice('整批工卡只生成一份A4打印Word，每页10张、每张8.5×5.4厘米。缺照或信息有误时请在名单里补齐；不会自动漏掉员工。打印请选择实际大小100%，不要缩放。')]
        if self.photo_client is None:
            sections.append(notice('自动取照服务尚未配置。可以在当前会话上传照片并补到对应记录，或联系管理员配置自动取照。', 'warning'))
        if job.get('edit_error'):
            sections.append(notice(job['edit_error'], 'error'))
        documents = job.get('documents') or ([job['document']] if job.get('document') else [])
        # Legacy jobs may retain old preview/volume references internally;
        # never present them as the current single-file result.
        if job.get('print_format_version') != 3:
            documents = []
        summary = (f"已生成打印Word：{job['layout']['count']}张工卡，共{job['layout']['pages']}页，每页最多{job['layout']['columns'] * job['layout']['rows']}张。"
                   if documents else
                   f"共 {len(job['employees'])} 人，{ready} 人照片已就绪。请补齐待处理信息后生成打印文件。")
        result = card('employee-cards', job['id'], 'completed' if documents else 'needs_input', summary,
                      sections=sections, artifacts=documents, actions=actions)
        return await self._save_result(ctx, result)

    async def _cards(self, ctx, action, args):
        if self.assets_dir is None:
            raise ProjectFailure(503, 'card_assets_not_configured')
        if action == 'template':
            object_args(args, set())
            from openpyxl import Workbook
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = '工卡名单'
            worksheet.append(['工号', '姓名', '英文名', '照片'])
            for key in 'ABCD':
                worksheet.column_dimensions[key].width = 24
            worksheet.freeze_panes = 'A2'
            data = io.BytesIO()
            workbook.save(data)
            artifact = await self.files.write(ctx, '工卡名单模板.xlsx', data.getvalue(),
                          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            return await self._save_result(ctx, card('employee-cards', uuid4().hex, 'completed',
                       '请填写工号、姓名、英文名后将名单附加到本会话；照片列可留空。', artifacts=[artifact]))
        if action == 'import_workbook':
            object_args(args, {'attachment_id'}, {'attachment_id'})
            source = await self._attachment(ctx, args['attachment_id'], {'.xlsx'}, 10 * 1024 * 1024)
            from .card_importer import ImportValidationError, parse_workbook
            try:
                employees = await asyncio.to_thread(parse_workbook, source['data'])
            except ImportValidationError as exc:
                result = card('employee-cards', uuid4().hex, 'needs_input', str(exc), sections=[table('需要修改',
                              [{'key': k, 'label': label} for k, label in [('row', '行'), ('field', '字段'), ('message', '说明')]], exc.errors)])
                return await self._save_result(ctx, result)
            job = {'id': uuid4().hex, 'employees': [{**e, 'id': str(i + 1), 'selected': True, 'status': 'missing'}
                                                 for i, e in enumerate(employees)]}
            await self._fetch_photos(ctx, job)
            await self.state.put(ctx, 'cards:' + job['id'], job)
            await self._card_auto_print(ctx, job)
            await self.state.put(ctx, 'cards:' + job['id'], job)
            return await self._card_result(ctx, job)
        allowed = {'review': {'job_id', 'offset'}, 'edit_employee': {'job_id', 'employee_id', 'name', 'english_name', 'selected', 'photo_attachment_id'},
                   'retry_photos': {'job_id'}, 'generate': {'job_id', 'layout'}}
        object_args(args, allowed[action], {'job_id'})
        job = await self._job(ctx, args['job_id'])
        if action == 'review':
            offset = args.get('offset', 0)
            if type(offset) is not int or not 0 <= offset <= 500:
                raise ProjectFailure(400, 'project_page_invalid')
            return await self._card_result(ctx, job, offset)
        if action == 'edit_employee':
            original = next((e for e in job['employees'] if e['id'] == args.get('employee_id')), None)
            if original is None:
                raise ProjectFailure(404, 'project_employee_not_found')
            employee = copy.deepcopy(original)
            for key, maximum in [('name', 24), ('english_name', 64)]:
                if key in args:
                    value = args[key]
                    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum or any(ord(c) < 32 for c in value):
                        raise ProjectFailure(400, 'project_employee_text_invalid')
                    employee[key] = value.strip()
            if 'selected' in args:
                if type(args['selected']) is not bool:
                    raise ProjectFailure(400, 'project_selection_invalid')
                employee['selected'] = args['selected']
            if 'photo_attachment_id' in args:
                source = await self._attachment(ctx, args['photo_attachment_id'], {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}, 8 * 1024 * 1024)
                from .card_photos import normalize_photo, PhotoError
                try:
                    data = await asyncio.to_thread(normalize_photo, source['data'])
                except PhotoError:
                    raise ProjectFailure(400, 'project_photo_invalid') from None
                employee['photo'] = await self.files.write(ctx, employee['employee_no'] + '-照片.png', data, 'image/png')
            if employee.get('selected') and employee.get('photo') and (employee.get('status') != 'ready' or set(args) & {'name', 'english_name', 'photo_attachment_id'}):
                from .card_renderer import CardValidationError
                photo = await self._attachment(ctx, employee['photo']['attachment_id'], {'.png'}, 10 * 1024 * 1024)
                try:
                    await self._card_validate(ctx, employee, photo['data'])
                except CardValidationError as exc:
                    job['edit_error'] = f"第{original['id']}条修改未保存：" + text(exc, 300)
                    await self.state.put(ctx, 'cards:' + job['id'], job)
                    return await self._card_result(ctx, job)
            original.update(employee)
            job.pop('edit_error', None)
            job.pop('document', None)
            job.pop('documents', None)
            job.pop('archive', None)
            await self.state.put(ctx, 'cards:' + job['id'], job)
            await self._card_auto_print(ctx, job)
        elif action == 'retry_photos':
            await self._fetch_photos(ctx, job)
            await self.state.put(ctx, 'cards:' + job['id'], job)
            await self._card_auto_print(ctx, job)
        elif action == 'generate':
            selected = [e for e in job['employees'] if e['selected']]
            if not selected or any(not e.get('photo') for e in selected):
                raise ProjectFailure(409, 'project_selected_photo_missing')
            layout = args.get('layout', {})
            object_args(layout, {'card_width_mm', 'card_height_mm', 'gap_mm', 'page_margin_mm'})
            for key, value in layout.items():
                if (type(value) not in {int, float} or not math.isfinite(value) or
                        not CARD_LAYOUT[key][0] <= value <= CARD_LAYOUT[key][1]):
                    raise ProjectFailure(400, 'project_card_layout_invalid')
            from .card_batch import generate_print
            await generate_print(self, ctx, job, selected, layout)
        await self.state.put(ctx, 'cards:' + job['id'], job)
        return await self._card_result(ctx, job)

    async def _heatcost(self, ctx, action, args):
        if action == 'prepare_drawing':
            from .heatcost_flow import prepare_drawing
            return await prepare_drawing(self, ctx, args)
        if action == 'drawing_requirements':
            object_args(args, set())
            result = await self._save_result(ctx, card('heatcost', 'drawing-requirements', 'needs_input',
                '请随本条消息上传PDF、PNG、JPG、WebP、BMP或TIFF图纸；每份最多分析前三页。DWG/DXF请先导出PDF。', sections=[
                    notice('上传后按图纸明确标注提取字段和工艺，再计算参考成本。未识别字段保持空缺，不按文件名套用样例或编造价格。'),
                    table('识别字段', [{'key': 'label', 'label': '字段'}, {'key': 'unit', 'label': '单位'}],
                          [{'label': label, 'unit': unit} for label, unit in FIELDS.values()])]))
            return {**result, 'instruction': '使用当前会话已选视觉模型读取本次图纸，逐字段列出依据。未知值传null，不能按文件名套用样例。',
                    'fields': {key: {'label': label, 'unit': unit} for key, (label, unit) in FIELDS.items()},
                    'processes': list(PROCESS_RATES), 'next_action': 'calculate',
                    'evidence_shape': {'value': None, 'evidence': '图纸页码与明确标注', 'confidence': 0.0}}
        from .heatcost_actions import invoke_cost
        return await invoke_cost(self, ctx, action, args)

    async def _login_card(self):
        if self.monitor is None: raise ProjectFailure(503, 'monitor_service_not_configured')
        try: locations = await self.monitor.locations()
        except MonitorFailure as exc: raise ProjectFailure(502, exc.code) from None
        return card('infrastructure-monitor', 'monitor', 'needs_input', '请连接需要查看的集群。账号和密码仅提交给集群服务，不进入聊天。',
                    actions=[{'id': 'monitor-login', 'label': '连接集群', 'kind': 'secure_form',
                              'form': 'monitor-login', 'connector': 'infrastructure-monitor',
                              'fields': [{'name': 'location', 'label': '集群', 'type': 'select',
                                          'options': locations},
                                         {'name': 'target', 'label': 'vCenter IP 地址（仅本次连接）', 'type': 'text'},
                                         {'name': 'username', 'label': '集群账号', 'type': 'text'},
                                         {'name': 'password', 'label': '密码', 'type': 'password'}]}])

    async def secure_login(self, ctx, fields):
        """Human-only endpoint. MUST NOT be registered as a model tool."""
        await self._authorize(ctx, 'infrastructure-monitor')
        object_args(fields, {'username', 'password', 'location', 'target'}, {'username', 'password', 'location'})
        if self.monitor is None:
            raise ProjectFailure(503, 'monitor_service_not_configured')
        async with self.state.lock(ctx):
            await self._authorize(ctx, 'infrastructure-monitor')
            try:
                data, cookie = await self.monitor.login(fields['username'], fields['password'], fields['location'], fields.get('target'))
            except MonitorFailure as exc:
                raise ProjectFailure(401 if exc.code in {'monitor_login_required', 'monitor_credentials_rejected'} else 502, exc.code) from None
            await self._authorize(ctx, 'infrastructure-monitor')
            await self.state.delete(ctx, 'monitor-cache:performance')
            await self.state.delete(ctx, 'monitor-cache:disk')
            await self.state.put(ctx, 'monitor-grant', {'cookie': cookie, 'location': fields['location'], 'expires': self.clock() + 840})
            await self.state.put(ctx, 'monitor-cache:performance', {'data': data, 'time': self.clock(), 'location': fields['location']})
            # Store projected output only; never username/password/cookie in results.
            return await self._monitor_result(ctx, data, 'performance')

    async def _monitor(self, ctx, action, args):
        object_args(args, {'kind', 'section', 'offset'} if action == 'view' else ({'kind'} if action == 'refresh' else set()))
        if action == 'disconnect':
            await self.state.delete(ctx, 'monitor-grant')
            await self.state.delete(ctx, 'monitor-cache:performance')
            await self.state.delete(ctx, 'monitor-cache:disk')
            return await self._save_result(ctx, await self._login_card())
        grant = await self.state.get(ctx, 'monitor-grant')
        if not grant or grant['expires'] <= self.clock():
            await self.state.delete(ctx, 'monitor-grant')
            return await self._save_result(ctx, await self._login_card())
        if action == 'status':
            result = card('infrastructure-monitor', 'monitor', 'completed', '当前连接：' + LOCATIONS[grant['location']],
                          actions=[invoke_action('刷新性能', 'infrastructure-monitor', 'refresh', {'kind': 'performance'}),
                                   invoke_action('磁盘健康', 'infrastructure-monitor', 'refresh', {'kind': 'disk'}),
                                   invoke_action('断开连接', 'infrastructure-monitor', 'disconnect', {})])
            return await self._save_result(ctx, result)
        kind = args.get('kind', 'performance')
        if kind not in {'performance', 'disk'}:
            raise ProjectFailure(400, 'monitor_action_invalid')
        if self.monitor is None:
            raise ProjectFailure(503, 'monitor_service_not_configured')
        cache_key = 'monitor-cache:' + kind
        cached = await self.state.get(ctx, cache_key)
        if action == 'view':
            section, offset = args.get('section'), args.get('offset')
            permitted = {'host_resources', 'vm_resources', 'storage_resources', 'alert_resources'} if kind == 'performance' else {key for key, _, _ in DISK_SECTIONS}
            if section not in permitted or type(offset) is not int or not 0 <= offset <= 100000:
                raise ProjectFailure(400, 'project_page_invalid')
            if not cached or cached['location'] != grant['location']:
                raise ProjectFailure(409, 'monitor_refresh_required')
            return await self._monitor_result(ctx, cached['data'], kind, collected_at=cached['time'], section=section, offset=offset)
        if cached and cached['location'] == grant['location'] and self.clock() - cached['time'] < 20:
            return await self._monitor_result(ctx, cached['data'], kind, collected_at=cached['time'])
        try:
            data = await self.monitor.collect(grant['cookie'], kind)
        except MonitorFailure as exc:
            if exc.code == 'monitor_login_required':
                await self.state.delete(ctx, 'monitor-grant')
                return await self._save_result(ctx, await self._login_card())
            raise ProjectFailure(502, exc.code) from None
        await self._authorize(ctx, 'infrastructure-monitor')
        now = self.clock()
        grant['expires'] = now + 840
        await self.state.put(ctx, 'monitor-grant', grant)
        await self.state.put(ctx, cache_key, {'data': data, 'time': now, 'location': grant['location']})
        return await self._monitor_result(ctx, data, kind, collected_at=now)

    async def _monitor_result(self, ctx, data, kind, collected_at=None, section=None, offset=0):
        if not isinstance(data, dict) or not isinstance(data.get('summary'), dict):
            raise ProjectFailure(502, 'monitor_response_invalid')
        summary = data['summary']
        definitions = [('vm_count', '虚拟机', ''), ('host_count', '宿主机', ''), ('alert_count', '告警', ''), ('max_latency', '最大延迟', 'ms')]
        if kind == 'disk':
            definitions = [('host_total', '主机', ''), ('disk_total', '磁盘', ''), ('ssd_total', 'SSD', ''), ('hdd_total', 'HDD', ''),
                           ('unknown_media_total', '未知介质', ''), ('smart_record_returned', 'SMART返回', ''), ('smart_record_attached', 'SMART关联', ''),
                           ('endurance_available', '耐久指标可用', ''), ('smart_unmatched', '未匹配SMART', ''),
                           ('multipath_issues', '多路径问题', ''), ('collection_errors', '采集错误', '')]
        sections = [{'type': 'metrics', 'items': [{'label': label, 'value': cell(summary.get(key)), 'unit': unit} for key, label, unit in definitions]}]
        sources = [('host_resources', '宿主机', [('name', '主机'), ('cpu_pct', 'CPU %'), ('mem_pct', '内存 %'), ('status', '状态')]),
                   ('vm_resources', '虚拟机', [('name', '名称'), ('host', '宿主机'), ('cpu_pct', 'CPU %'), ('mem_pct', '内存 %'), ('storage_lat', '延迟ms'), ('status', '状态')]),
                   ('storage_resources', '存储与vSAN', [('name', '名称'), ('type', '类型'), ('capacity_gb', '容量GB'), ('used_pct', '已用%'), ('status', '状态')]),
                   ('alert_resources', '告警', [('level', '级别'), ('type', '类型'), ('name', '对象'), ('detail', '详情'), ('status', '状态')])]
        if kind == 'disk':
            sources = DISK_SECTIONS
            metadata = data.get('smart') if isinstance(data.get('smart'), dict) else {}
            sections.append(notice('SMART接口：' + text(metadata.get('state') or '未返回', 100) +
                                   '；来源：' + text(metadata.get('source') or '未返回', 100) +
                                   '；返回记录：' + str(metadata.get('disks_returned', '未返回')) +
                                   '；耐久数据：' + str(metadata.get('endurance_available', '未返回')) +
                                   ('；采集说明：' + text(metadata.get('error'), 500) if metadata.get('error') else '')))
            try:
                data = project_disk(data)
            except ValueError:
                raise ProjectFailure(502, 'monitor_response_invalid') from None
            levels = [row.get('endurance_level') for row in data.get('smart_health', [])]
            sections.append(notice('固态耐久报警：严重 ' + str(levels.count('critical')) + ' 项，预警 ' + str(levels.count('warning')) + ' 项，无法评估 ' + str(levels.count('unknown')) + ' 项。', 'warning' if 'critical' in levels or 'warning' in levels else 'info'))
            sections.append(notice('默认耐久预警为已用80%，严重预警90%；达到100%表示额定耐久估计已耗尽，并不等于设备自动只读。只读仅按明确设备信号显示。此看板只提示，不修改磁盘写入模式；阈值由管理员配置。'))
            sections.append(notice('接口原始值是当前所列耐久指标的设备报告值，不是磁盘已用空间。寿命剩余表示预计还剩多少额定写入耐久，寿命已用表示已消耗多少；标注“换算”的值由同一指标计算，不是另一次测量。'))
            sections.append(notice('容量按GiB显示。物理磁盘的耐久度列显示剩余与已用比例；非固态不计算闪存寿命。未返回、接口不可用与设备不支持分别保留，缺失不代表正常。非固态可能是机械盘或非SSD后端，未知介质不作推定。vSAN状态来自缓存快照，与SMART耐久度不同；百分比不等于可用年数，也不单独保证设备健康。'))
        page_actions = []
        selected_section = (section or 'smart_health') if kind == 'disk' else section
        for key, title, cols in sources:
            if selected_section and key != selected_section:
                continue
            rows = data.get(key, [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ProjectFailure(502, 'monitor_response_invalid')
            if rows:
                current = table(title, [{'key': k, 'label': label} for k, label in cols], rows[offset:offset + 100])
                current.update(total_rows=len(rows), offset=offset, truncated=offset + 100 < len(rows))
                sections.append(current)
                if offset + 100 < len(rows):
                    action = invoke_action(title + '下一页', 'infrastructure-monitor', 'view', {'kind': kind, 'section': key, 'offset': offset + 100})
                    action['id'] = 'view-' + key
                    page_actions.append(action)
            elif kind == 'disk':
                sections.append(notice(title + '：本次没有返回记录。'))
        if kind == 'disk':
            # One selected table per assistant result; existing view actions
            # append another result without replacing the original. Nine table
            # sections + at most one next-page action + three controls <= 12.
            for key, title, _ in sources:
                if key != selected_section and data.get(key):
                    action = invoke_action('查看' + title, 'infrastructure-monitor', 'view', {'kind': kind, 'section': key, 'offset': 0})
                    action['id'] = 'show-' + key
                    page_actions.append(action)
        collected_at = collected_at or self.clock()
        sections.append(notice('采集时间：' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(collected_at)) + '；20秒内复用本会话的采集结果。'))
        return await self._save_result(ctx, card('infrastructure-monitor', 'monitor', 'completed',
                 '集群性能已更新。' if kind == 'performance' else '磁盘健康已更新。', sections=sections,
                 actions=page_actions + [invoke_action('刷新性能', 'infrastructure-monitor', 'refresh', {'kind': 'performance'}),
                          invoke_action('磁盘健康', 'infrastructure-monitor', 'refresh', {'kind': 'disk'}),
                          invoke_action('断开连接', 'infrastructure-monitor', 'disconnect', {})]))
