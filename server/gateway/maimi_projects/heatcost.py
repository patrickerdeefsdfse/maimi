"""Deterministic cost engine derived from liuqinghuai; never calls a second LLM.

The existing drawing model supplies evidence-bearing fields via the selected
skill. Missing fields stay missing. The original example price rules are
explicitly reference-only, not a company-approved quote.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

RULE_VERSION = 'liuqinghuai-reference-20260924-v1'
MATERIAL_RULES = {
    'galvanized-05': ('0.5镀锌卷料', ('0.5镀锌',), '5.80', '4.00'),
    'galvanized-04': ('0.4镀锌卷料', ('0.4镀锌',), '6.10', '4.00'),
    'al6063-small': ('AL6063（小）', ('al6063小', 'al6063（小', 'al6063(小'), '22.86', '4.00'),
    'al6063': ('AL6063', ('al6063', '6063t5'), '28.80', '4.00'),
    'sus304': ('SUS304', ('sus304', '304不锈钢', '不锈钢304'), '21.00', '15.00'),
    'copper': ('铜', ('铜', 'copper'), '75.70', '0.00'),
    'other': ('其他材料', ('其他材料', 'othermaterial'), '27.50', '0.00'),
}
PROCESS_RATES = {'cutting': '0.12', 'drilling': '0.10', 'tapping': '0.05',
                 'milling': '0', 'cnc': '50', 'anodize': '0.10',
                 'cleaning': '0.01', 'deburr': '0.50', 'marking': '0.02'}
PROCESS_NAMES = dict(zip(PROCESS_RATES, ('切割', '钻孔', '攻牙', '铣削', 'CNC', '阳极氧化', '清洗', '去毛刺', '标识')))
FIELDS = {'partNumber': ('料号', ''), 'model': ('规格型号', ''), 'material': ('材料', ''),
          'length': ('最大长度', 'mm'), 'height': ('最大高度', 'mm'), 'width': ('截面宽度', 'mm'),
          'weight': ('产品重量', 'g'), 'finCount': ('鳍片数', 'pcs'), 'holeCount': ('孔数', 'pcs')}


def number(value, *, maximum='1000000000', nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('无效数值')
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value < 0 or value > Decimal(maximum):
            raise ValueError('数值超出范围')
        return value
    except InvalidOperation:
        raise ValueError('无效数值') from None


def normalized(value):
    return re.sub(r'[\s_\-/]', '', str(value).lower())


def money(value):
    return float(value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))


def calculate(fields, processes, rates=None, *, scrap_weight_g=12):
    if not isinstance(fields, dict) or set(fields) - set(FIELDS):
        raise ValueError('字段不符合图纸识别格式')
    if not isinstance(processes, dict) or set(processes) - set(PROCESS_RATES):
        raise ValueError('加工工艺无效')
    rates = dict(rates or {})
    if set(rates) - {*PROCESS_RATES, 'material', 'scrap', 'packaging', 'rent'}:
        raise ValueError('价格字段无效')
    provided = {key: number(value, maximum='1000000') for key, value in rates.items()}
    values, field_rows = {}, []
    for key, (label, unit) in FIELDS.items():
        entry = fields.get(key, {})
        if not isinstance(entry, dict) or set(entry) - {'value', 'evidence', 'confidence'}:
            raise ValueError('字段必须带图纸值与依据')
        value = entry.get('value')
        if key in {'partNumber', 'model', 'material'}:
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError('图纸文本字段无效')
        else:
            value = number(value, nullable=True)
            value = float(value) if value is not None else None
            if key in {'finCount', 'holeCount'} and value is not None and not value.is_integer():
                raise ValueError('数量必须是整数')
        evidence = entry.get('evidence', '')
        if not isinstance(evidence, str) or len(evidence) > 1000:
            raise ValueError('字段依据过长')
        confidence = float(number(entry.get('confidence', 0), maximum='1'))
        values[key] = value
        field_rows.append({'field': label, 'key': key, 'value': value, 'unit': unit,
                           'evidence': evidence, 'confidence': confidence})
    matched = next(((key, rule) for key, rule in MATERIAL_RULES.items()
                    if any(normalized(alias) in normalized(values.get('material') or '') for alias in rule[1])), None)
    warnings = ['当前使用原项目参考规则，未经公司价格审批，结果需人工复核。',
                '废料、利润、管理费和税费沿用参考模型；正式报价应提供已批准的规则版本。']
    ready = bool(values.get('weight') and (matched or 'material' in provided))
    if not ready:
        warnings.append('缺少有效重量或匹配的材料规则，暂不计算总价。')
    resolved = {key: Decimal(value) for key, value in PROCESS_RATES.items()}
    resolved.update({'material': Decimal(matched[1][2]) if matched else Decimal(0),
                     'scrap': Decimal(matched[1][3]) if matched else Decimal(0),
                     'packaging': Decimal('.30'), 'rent': Decimal('.05')})
    resolved.update(provided)
    rows, processing = [], Decimal(0)
    for key, label in PROCESS_NAMES.items():
        entry = processes.get(key)
        if entry is None:
            warnings.append(f'{label}未确认，不计入当前参考金额。')
            rows.append({'item': label, 'amount': None, 'evidence': '待确认', 'key': key, 'basis': None, 'rate': float(resolved[key])})
            continue
        if not isinstance(entry, dict) or set(entry) - {'enabled', 'billable', 'evidence'}:
            raise ValueError('工艺字段格式无效')
        if type(entry.get('enabled')) is not bool or type(entry.get('billable', True)) is not bool:
            raise ValueError('工艺开关必须为布尔值')
        evidence = entry.get('evidence', '')
        if not isinstance(evidence, str) or len(evidence) > 1000:
            raise ValueError('工艺依据过长')
        amount = resolved[key] if entry['enabled'] and entry.get('billable', True) else Decimal(0)
        if key == 'cleaning':
            amount *= Decimal('.15')
        processing += amount
        rows.append({'key': key, 'item': label, 'amount': money(amount), 'evidence': evidence,
                     'basis': ('.15' if key == 'cleaning' else '1') if entry['enabled'] and entry.get('billable', True) else '0', 'rate': float(resolved[key])})
    totals = None
    detail = [{'key': 'material', 'item': '材料净额（扣废料）', 'amount': None, 'rate': float(resolved['material']) if matched or 'material' in provided else None,
               'basis': '重量/1000×材料单价－废料重量/1000×废料单价', 'evidence': matched[1][0] if matched else '材料规则未匹配'}] + rows
    if ready:
        scrap = number(scrap_weight_g, maximum='1000000')
        weight = number(values['weight'])
        if scrap > weight:
            raise ValueError('废料重量不能超过计价重量')
        material = max(Decimal(0), weight / 1000 * resolved['material'] - scrap / 1000 * resolved['scrap'])
        direct = material + processing
        profit = direct * Decimal('.05')
        packaging, rent = resolved['packaging'], resolved['rent']
        management = (direct + profit + packaging + rent) * Decimal('.03')
        tax = (processing + profit + packaging + rent) * Decimal('.13')
        indirect = profit + tax + packaging + management + rent
        totals = {key: money(value) for key, value in {'material': material, 'processing': processing,
                  'indirect': indirect, 'total': direct + indirect}.items()}
        detail[0]['amount'] = money(material)
        detail[0]['basis'] = f'{weight}/1000×{resolved["material"]}－{scrap}/1000×{resolved["scrap"]}'
        detail.extend({'key': key, 'item': label, 'amount': money(value), 'basis': basis, 'rate': rate, 'evidence': '原项目参考规则'}
                      for key, label, value, basis, rate in [
                          ('profit', '利润', profit, '（材料＋加工）×5%', .05),
                          ('packaging', '包装', packaging, '固定元/件', float(packaging)),
                          ('rent', '租金', rent, '固定元/件', float(rent)),
                          ('management', '管理费', management, '（直接成本＋利润＋包装＋租金）×3%', .03),
                          ('tax', '税费', tax, '（加工＋利润＋包装＋租金）×13%', .13)])
    else:
        detail.extend({'key': key, 'item': label, 'amount': None, 'basis': '关键计价字段待确认', 'rate': None, 'evidence': '待确认'}
                      for key, label in [('profit', '利润'), ('packaging', '包装'), ('rent', '租金'), ('management', '管理费'), ('tax', '税费')])
    return {'fields': field_rows, 'cost_lines': rows, 'totals': totals, 'warnings': warnings,
            'detail_lines': detail,
            'pricing_ready': ready, 'rule_version': RULE_VERSION, 'approved_quote': False,
            'rates': {k: float(v) for k, v in resolved.items()}, 'scrap_weight_g': scrap_weight_g}
