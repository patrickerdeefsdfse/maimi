"""Business adapters for selected DSH conversation tools.

The gateway supplies a server-authenticated Context and reauthorization hook.
No identity, location, output path, endpoint or credential is accepted from a
model argument. ``state`` is an encrypted, owner+session scoped store with a
distributed lock; ``files`` is the existing DSH attachment/workspace boundary.
Both ports must enforce the context again on every read/write. No independent
login, chat, upload store, AI key or browser application is introduced here.
"""
from __future__ import annotations

from app.site_access import configured_staff_group

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .contracts import NAMES, KEYS, OPAQUE, card, table, notice, invoke_action, text, cell
from .heatcost import calculate, FIELDS, PROCESS_RATES
from .monitor import LOCATIONS, MonitorFailure
from .monitor_projection import DISK_SECTIONS, project_disk
from .server_operations import TOOLS as SERVICE_TOOLS, USERS as SERVICE_USERS, ServiceFailure

VERSION = '1.0.1'
MONITOR_USERS = frozenset(configured_staff_group('MAIMI_AUTHORIZED_STAFF_GROUP_2'))
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


from . import sales_orders, order_approvals
TOOLS['employee-badges'] = [
    ('template', '下载可填写的工卡名单模板', schema()),
    ('generate', '从当前会话的名单和获准的员工照片生成可打印 Word', schema({
        'attachment_id': REF_SCHEMA,
        'allow_partial': {'type':'boolean','default':False},
        'photo_overrides': {'type':'array','maxItems':20,'items':schema({
            'employee_no': {'type':'string','pattern':'^M[0-9]{8}$'},'attachment_id':REF_SCHEMA
        },('employee_no','attachment_id'))}
    }, ('attachment_id',)))
]
TOOLS['service'] = SERVICE_TOOLS
TOOLS['sales-orders'] = sales_orders.TOOLS
TOOLS['sales-order-approval'] = order_approvals.TOOLS


class ProjectSkillService:
    def __init__(self, *, authorize, state, files, monitor=None, clock=time.time,
                 meeting_assets_dir=None, meeting_assets_manifest=None, drawing_text=None, progress=None, server_operations=None, sales_order_call=None):
        self.authorize, self.state, self.files = authorize, state, files
        self.monitor, self.clock = monitor, clock
        self.drawing_text = drawing_text
        self.progress = progress
        self.server_operations = server_operations
        self.sales_orders = sales_order_call
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
        if key and ctx.location == 'cloud':
            from app.plugin_admin import project_granted
            if not await asyncio.to_thread(project_granted, ctx.user_no, key):
                raise ProjectFailure(403,'project_not_authorized')
        if key and key not in KEYS:
            raise ProjectFailure(404, 'project_not_found')
        if key in {'sales-order-approval','employee-badges'} and ctx.location != 'cloud':
            raise ProjectFailure(403, 'project_not_authorized')
        if key == 'infrastructure-monitor' and (ctx.user_no not in MONITOR_USERS or ctx.location != 'cloud'):
            raise ProjectFailure(403, 'project_not_authorized')
        if key == 'service' and (ctx.user_no not in SERVICE_USERS or ctx.location != 'cloud'):
            raise ProjectFailure(403, 'project_not_authorized')
        if selected and key and key not in ctx.selected:
            raise ProjectFailure(403, 'project_not_selected')

    async def catalog(self, ctx):
        await self._authorize(ctx, selected=False)
        items = []
        if ctx.location == 'cloud':
            from app.plugin_admin import inventory
            grants = {r['plugin_key'] for r in await asyncio.to_thread(inventory,ctx.user_no) if r['effective'] and (r['plugin_key']!='employee-badges' or (r.get('kind')=='skill' and r.get('state')=='published'))}
        else:
            grants = set(KEYS)
        for key in KEYS:
            if key not in grants:
                continue
            if key in {'sales-order-approval','employee-badges'} and ctx.location != 'cloud':
                continue
            if key == 'infrastructure-monitor' and (ctx.user_no not in MONITOR_USERS or ctx.location != 'cloud'):
                continue
            if key == 'service' and (ctx.user_no not in SERVICE_USERS or ctx.location != 'cloud'):
                continue
            if key in {'sales-orders','sales-order-approval'} and self.sales_orders is None:
                continue
            status, reason = 'ready', ''
            if key == 'meeting' and self.meeting_assets is None:
                status, reason = 'limited', 'meeting_templates_not_configured'
            elif key == 'infrastructure-monitor' and self.monitor is None:
                status, reason = 'unavailable', 'monitor_service_not_configured'
            if key == 'service':
                ready, reason = self.server_operations.availability(ctx) if self.server_operations is not None else (False, 'project_service_not_configured')
                if not ready:status='unavailable'
            descriptions = {'service':'在对话中的连接区域填写 IP 和密码后，查看服务器环境、读写代码和执行任务；密码不进入聊天', 'meeting': '核对董事会会议清单，按公司母版生成签字页及成套Word',
                            'infrastructure-monitor': '在对话中查看集群性能和磁盘健康', 'heatcost': '从图纸提取字段并核算参考成本'}
            descriptions['employee-badges'] = '从已上传的 Excel 名单生成带照片的 Word 工卡。'
            descriptions['sales-orders'] = '从本会话上传的订单提取关键字段、复核建单，并在审核通过后自动归档。'
            descriptions['sales-order-approval'] = '查看 Oracle 分派给本人的待审销售订单，核对详情后由本人提交审核决定。'
            items.append({'plugin_key': key, 'version': VERSION, 'name': NAMES[key], 'description': descriptions[key],
                          'status': status, 'reason': reason, 'connect_url': None, 'authorization_url': None,
                          'tools': [{'name': action, 'description': description, 'input_schema': shape,
                                     'requires_confirmation': False, 'available': status == 'ready' or (key == 'meeting' and action not in {'generate', 'generate_from_workbook'})}
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
        if any(row.get('connector') == 'employee-badges' for row in results):
            from app.plugin_admin import project_granted
            if ctx.location != 'cloud' or not await asyncio.to_thread(project_granted,ctx.user_no,'employee-badges'):
                results = [row for row in results if row.get('connector') != 'employee-badges']
        if any(row.get('connector') == 'sales-order-approval' for row in results):
            from app.plugin_admin import project_granted
            if ctx.location != 'cloud' or not await asyncio.to_thread(project_granted,ctx.user_no,'sales-order-approval'):
                results = [row for row in results if row.get('connector') != 'sales-order-approval']
        return [r for r in results if r['connector'] in KEYS and (r['connector'] != 'infrastructure-monitor' or
                (ctx.user_no in MONITOR_USERS and ctx.location == 'cloud')) and
                (r['connector'] != 'service' or (ctx.user_no in SERVICE_USERS and ctx.location == 'cloud'))]

    async def invoke(self, ctx, connector, action, arguments):
        await self._authorize(ctx, connector)
        confirmed_order = connector == 'sales-orders' and action in {'confirm_order','prepare','submit','recognize_test','recognize_production'} and ctx.source == 'human'
        confirmed_approval = connector == 'sales-order-approval' and action == 'approve' and ctx.source == 'human'
        if not any(item[0] == action for item in TOOLS[connector]) and not confirmed_order and not confirmed_approval and not (connector == 'heatcost' and action == 'revise' and ctx.source == 'human'):
            raise ProjectFailure(404, 'project_action_not_found')
        async with self.state.lock(ctx):
            await self._authorize(ctx, connector)
            if connector == 'service':
                return await self._server_operation(ctx, action, arguments)
            if connector == 'sales-orders':
                return await sales_orders.invoke(self, ctx, action, arguments)
            if connector == 'sales-order-approval':
                return await order_approvals.invoke(self, ctx, action, arguments)
            if connector == 'heatcost':
                return await self._heatcost(ctx, action, arguments)
            if connector == 'infrastructure-monitor':
                return await self._monitor(ctx, action, arguments)
            if connector == 'employee-badges':
                from .badge_flow import invoke_badges
                return await invoke_badges(self,ctx,action,arguments)
            if connector == 'meeting':
                from .meeting_flow import invoke_meeting
                return await invoke_meeting(self, ctx, action, arguments)
        raise ProjectFailure(503, 'project_source_review_pending')

    async def _server_operation(self, ctx, action, arguments):
        if self.server_operations is None:
            raise ProjectFailure(503, 'project_service_not_configured')
        try:
            value = await self.server_operations.invoke(ctx, action, arguments)
        except ServiceFailure as exc:
            if exc.code in {'project_service_connection_required','project_service_ssh_session_expired'}:
                await self._authorize(ctx,'service')
                return self._service_connection_card({'protocol':'maimi-service-session-v2','connected':False,'server':None})
            error = ProjectFailure(exc.status, exc.code)
            error.remote_status_unknown = exc.remote_status_unknown
            raise error from None
        await self._authorize(ctx, 'service')
        if action in {'status','list_servers','disconnect'}:
            return self._service_connection_card(value)
        title = {'inspect_server':'服务器检查已完成','read_file':'代码文件已读取','write_file':'代码文件已写入并校验','run_command':'服务器命令已返回'}[action]
        sections = []
        if action == 'inspect_server':
            sections.append(table('检查结果', [{'key':'name','label':'检查项'},{'key':'status','label':'状态'},{'key':'exit_code','label':'退出码'}], value['checks'], maximum=32))
            for check in value['checks'][:8]:
                sections.append(notice(check['name'] + '\n' + text(check['output'], 1800)))
        elif action in {'read_file','write_file'}:
            sections.append(notice(value['relative_path'] + '；SHA256：' + value['sha256']))
            if action == 'read_file':sections.append(notice(text(value['content'], 1800)))
        else:
            sections.append(notice('退出码：' + str(value['exit_code']) + ('；达到超时限制' if value['timed_out'] else '')))
            sections.append(notice(text(value['output'], 1800)))
            if value['high_privilege']:sections.append(notice('本次使用已授权的服务器管理员权限执行；该运行环境不是隔离沙箱。'))
        if value.get('truncated'):sections.append(notice('输出已达到长度限制；不得据此声称未展示的部分执行成功。'))
        result = card('service', uuid4().hex, 'completed', title, sections=sections)
        saved = await self._save_result(ctx, result)
        return {**saved, 'service_result':value, 'untrusted_external_content':True}

    @staticmethod
    def _service_connection_card(value):
        # Connection authority lives only in the broker. Do not save a grant
        # or a persistent live-status card into the project state result list.
        if not value['connected']:
            result=card('service','service-connection','needs_input','请在下方填写服务器 IP 和密码，再点击连接。密码不进入聊天。',
                actions=[{'id':'service-login','label':'连接服务器','kind':'secure_form','form':'service-login','connector':'service',
                    'fields':[{'name':'host','label':'服务器 IP 地址','type':'text'},
                              {'name':'password','label':'密码','type':'password'}]}])
        else:
            result=card('service','service-connection','completed','本次检查已连接：'+value['server']['label']+'。请直接描述需要完成的任务。',
                actions=[invoke_action('检查连接','service','status',{}),invoke_action('断开连接','service','disconnect',{})])
        return {'status':result['status'],'project_result':result,'service_result':value,'untrusted_external_content':True}

    async def secure_connect(self,ctx,fields):
        """Human form only. Never register this method in the model catalog."""
        await self._authorize(ctx,'service')
        if ctx.source!='human':raise ProjectFailure(403,'project_human_authorization_required')
        if self.server_operations is None:raise ProjectFailure(503,'project_service_not_configured')
        async with self.state.lock(ctx):
            await self._authorize(ctx,'service')
            try:value=await self.server_operations.secure_connect(ctx,fields)
            except ServiceFailure as exc:
                error=ProjectFailure(exc.status,exc.code);error.remote_status_unknown=exc.remote_status_unknown
                raise error from None
            await self._authorize(ctx,'service')
            return self._service_connection_card(value)

    async def _attachment(self, ctx, attachment_id, suffixes, maximum):
        # Files port must resolve and authorize an opaque reference, never a
        # filesystem path or arbitrary URL. Shared upload path remains HTTPS443.
        value = await self.files.read(ctx, ref(attachment_id), maximum)
        if (not isinstance(value, dict) or not isinstance(value.get('name'), str) or
                not isinstance(value.get('data'), bytes) or len(value['data']) > maximum or
                Path(value['name']).suffix.lower() not in suffixes):
            raise ProjectFailure(400, 'project_attachment_invalid')
        return value

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
