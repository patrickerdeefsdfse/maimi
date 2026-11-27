"""Deterministic analysis persistence, human corrections and safe CSV output."""
from __future__ import annotations

import copy
import csv
import io
from uuid import uuid4

from .contracts import card, table, notice
from .heatcost import calculate, FIELDS, PROCESS_NAMES, PROCESS_RATES, number

RATE_LABELS = {'material': '材料单价（元/kg）', 'scrap': '废料单价（元/kg）',
               **PROCESS_NAMES, 'packaging': '包装（元/件）', 'rent': '租金（元/件）'}


def csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, (int, float)):
        return value
    value = str(value).replace('\x00', '')
    # CSV quoting alone does not prevent spreadsheet formula execution.
    if value.lstrip(' \t\r\n').startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        value = "'" + value
    return value


def export_csv(calculation):
    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\r\n')
    def row(values):
        writer.writerow([csv_cell(v) for v in values])
    row(['规则版本', calculation['rule_version'], '参考计算，未经公司报价审批'])
    row(['字段', '识别值', '单位', '依据'])
    for field in calculation['fields']:
        row([field['field'], field['value'], field['unit'], field['evidence']])
    row([])
    row(['项目', '计价基准', '单价/系数', '金额（元/件）', '依据'])
    for item in calculation['detail_lines']:
        row([item['item'], item['basis'], item['rate'], item['amount'], item['evidence']])
    row(['废料重量（g）', calculation['scrap_weight_g']])
    if calculation['totals']:
        for key, label in [('material', '材料小计'), ('processing', '加工小计'), ('indirect', '间接费用小计'), ('total', '参考合计')]:
            row([label, '', '', calculation['totals'][key]])
    for warning in calculation['warnings']:
        row(['说明', warning])
    return ('\ufeff' + out.getvalue()).encode('utf-8')


def forms(analysis_id):
    forms = []
    for kind, label, keys in [('field', '修改图纸字段', {k: v[0] for k, v in FIELDS.items()}),
                              ('rate', '修改参考单价', RATE_LABELS), ('process', '修改加工工艺', PROCESS_NAMES)]:
        value = {'name': 'value', 'label': '新值（留空清除字段）' if kind == 'field' else '新值',
                 'type': 'text' if kind == 'field' else 'number', 'required': kind != 'field'}
        if kind == 'rate':
            value.update(minimum=0, maximum=1000000, step='any')
        if kind == 'process':
            value = {'name': 'value', 'label': '是否采用该工艺', 'type': 'select', 'required': True,
                     'value_type': 'boolean', 'options': [{'value': 'true', 'label': '采用'}, {'value': 'false', 'label': '不采用'}]}
        forms.append({'id': 'cost-edit-' + kind, 'label': label, 'kind': 'project_form', 'form': 'cost-edit',
                      'mode': kind, 'connector': 'heatcost', 'action': 'revise', 'arguments': {'analysis_id': analysis_id, 'kind': kind},
                      'fields': [{'name': 'key', 'label': '修改项目', 'type': 'select', 'required': True,
                                  'options': [{'value': k, 'label': v} for k, v in keys.items()]}, value]})
    return forms


async def render(service, ctx, analysis):
    from .service import ProjectFailure
    try:
        calculation = calculate(analysis['input']['fields'], analysis['input']['processes'], analysis['input'].get('rates'),
                                scrap_weight_g=analysis['input'].get('scrap_weight_g', 12))
    except (ValueError, TypeError):
        raise ProjectFailure(400, 'project_cost_fields_invalid') from None
    await service._authorize(ctx, 'heatcost')
    if not analysis.get('csv'):
        analysis['csv'] = await service.files.write(ctx, '图纸成本明细.csv', export_csv(calculation), 'text/csv; charset=utf-8')
    analysis['calculation'] = calculation
    await service.state.put(ctx, 'heatcost:' + analysis['id'], analysis)
    await service.state.put(ctx, 'heatcost-latest', analysis['id'])
    sections = [table('图纸字段与依据', [{'key': k, 'label': label} for k, label in
                [('field', '字段'), ('value', '值'), ('unit', '单位'), ('evidence', '依据')]], calculation['fields']),
                table('参考成本明细', [{'key': k, 'label': label} for k, label in
                [('item', '项目'), ('basis', '计价基准'), ('rate', '单价/系数'), ('amount', '金额（元/件）'), ('evidence', '依据')]], calculation['detail_lines'])]
    if calculation['totals']:
        sections.insert(0, {'type': 'metrics', 'items': [{'label': label, 'value': calculation['totals'][key], 'unit': '元/件'}
                         for key, label in [('material', '材料'), ('processing', '加工'), ('indirect', '间接费用'), ('total', '参考合计')]]})
    sections.extend(notice(warning, 'warning') for warning in calculation['warnings'])
    sections.append(notice('可在下方表单修改单个字段、单价或工艺后重算；修改依据会标记为人工确认。'))
    return await service._save_result(ctx, card('heatcost', analysis['id'], 'needs_input',
        '已按图纸字段计算参考成本，请复核字段与价格规则。' if calculation['pricing_ready'] else '关键字段待补充，暂不显示总价。',
        sections=sections, artifacts=[analysis['csv']], actions=forms(analysis['id'])))


async def invoke_cost(service, ctx, action, args):
    from .service import ProjectFailure, object_args, ref
    if action == 'review':
        object_args(args, {'analysis_id'})
        analysis_id = ref(args['analysis_id']) if 'analysis_id' in args else await service.state.get(ctx, 'heatcost-latest')
        if not analysis_id:
            return await service._heatcost(ctx, 'drawing_requirements', {})
        analysis = await service.state.get(ctx, 'heatcost:' + analysis_id)
        if not analysis:
            raise ProjectFailure(404, 'project_cost_analysis_not_found')
        return await render(service, ctx, analysis)
    if action == 'revise':
        if ctx.source != 'human':
            raise ProjectFailure(403, 'project_cost_human_revision_required')
        object_args(args, {'analysis_id', 'kind', 'key', 'value'}, {'analysis_id', 'kind', 'key', 'value'})
        analysis = await service.state.get(ctx, 'heatcost:' + ref(args['analysis_id']))
        if not analysis:
            raise ProjectFailure(404, 'project_cost_analysis_not_found')
        analysis = copy.deepcopy(analysis)
        value, key, kind = args['value'], args['key'], args['kind']
        if not isinstance(key, str) or not isinstance(kind, str):
            raise ProjectFailure(400, 'project_cost_revision_invalid')
        try:
            if kind == 'field' and key in FIELDS:
                if value is not None and (not isinstance(value, (str, int, float)) or isinstance(value, bool)):
                    raise ValueError()
                if key in {'partNumber', 'model', 'material'} and value is not None and not isinstance(value, str):
                    raise ValueError()
                analysis['input']['fields'][key] = {'value': value, 'evidence': '人工确认修改', 'confidence': 1}
            elif kind == 'rate' and key in RATE_LABELS:
                analysis['input'].setdefault('rates', {})[key] = float(number(value, maximum='1000000'))
            elif kind == 'process' and key in PROCESS_RATES and type(value) is bool:
                previous = analysis['input']['processes'].get(key, {})
                analysis['input']['processes'][key] = {'enabled': value, 'billable': previous.get('billable', True), 'evidence': '人工确认修改'}
            else:
                raise ValueError()
        except (ValueError, TypeError):
            raise ProjectFailure(400, 'project_cost_revision_invalid') from None
        analysis.pop('csv', None)
        return await render(service, ctx, analysis)
    object_args(args, {'attachment_id', 'fields', 'processes', 'rates', 'scrap_weight_g'}, {'attachment_id', 'fields', 'processes'})
    if ctx.source != 'human' and args.get('rates'):
        raise ProjectFailure(403, 'project_cost_human_revision_required')
    await service._attachment(ctx, args['attachment_id'], {'.pdf', '.png', '.jpg', '.jpeg'}, 20 * 1024 * 1024)
    return await render(service, ctx, {'id': uuid4().hex, 'input': copy.deepcopy(args)})
