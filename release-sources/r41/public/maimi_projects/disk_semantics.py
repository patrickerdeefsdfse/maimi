"""Pure SMART interpretation shared by collector and conversation projection.

No I/O, API access, device-name inference or business identifiers are required.
Only explicitly named metrics have a percentage interpretation. All other
vendor values remain raw. Capacity values from the old collector use GiB.
"""
from __future__ import annotations

import math
import re

MWI = frozenset({'smartmediawearoutindicator', 'mediawearoutindicator', 'mediawearout'})
USED = frozenset({'percentageused', 'percentused', 'percentageusedenduranceindicator'})
HEALTH = frozenset({'smarthealthstatus', 'healthstatus'})


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value):
            return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def metric_key(value):
    return re.sub(r'[^a-z0-9]', '', value.lower()) if isinstance(value, str) else ''


def _display(value):
    parsed = number(value)
    if parsed is None:
        return '未返回'
    return format(parsed, '.12g')


def media_label(disk):
    flag = disk.get('is_ssd')
    # Never decide solid/non-solid from an arbitrary model/display name.
    if flag is True:
        return '固态（NVMe）' if disk.get('media') == 'NVMe' else '固态（SSD）'
    if flag is False:
        return '非固态（HDD/非SSD后端）'
    return '未知（接口未标明）'


def capacity_label(disk):
    value = number(disk.get('capacity_gb'))
    if value is None or value <= 0:
        return '未返回容量'
    return _display(value) + ' GiB'


def interpret_smart(smart, is_ssd=None):
    """Return a copy with coherent raw/remaining/used semantics.

    MWI uses the normalized percentage documented by Broadcom. It is not the
    separate raw counter displayed by localcli. A complementary percentage is
    marked as calculated, never presented as another observed counter.
    """
    result = dict(smart) if isinstance(smart, dict) else {}
    params = result.get('raw_params', [])
    if not isinstance(params, list):
        params = []
    candidates = []
    recognized = False
    invalid = False
    health_values = set()
    for row in params:
        if not isinstance(row, dict):
            continue
        key = metric_key(row.get('parameter'))
        raw = row.get('raw_value', row.get('value'))
        value = number(raw)
        if key in HEALTH:
            health_values.add({0: '正常', 1: '警告', 2: '即将故障', 3: '未知'}.get(value, '未知'))
        if key not in MWI | USED:
            continue
        recognized = True
        # Values absent from VMware must remain absent, including missing MWI.
        if value is None:
            invalid = raw not in (None, '', '-', 'N/A') or invalid
            continue
        limit = 100 if key in MWI else 255
        if not 0 <= value <= limit:
            invalid = True
            continue
        candidates.append((2 if key in MWI else 1, value, row, raw))

    result.update(metric='', raw_value=None, remaining_pct=None, used_pct=None,
                  raw_value_display='未返回', remaining_display='未返回', used_display='未返回',
                  interpretation='', remaining_origin='missing', used_origin='missing')
    if health_values:
        result['health'] = next(iter(health_values)) if len(health_values) == 1 else '未知（返回值冲突）'
    if result.get('health') in (None, '', '-'):
        result['health'] = '未返回'
    original_state = result.get('state') or '未返回'
    # The released collector labeled an empty successful reply unsupported.
    # An empty result alone cannot establish that the hardware lacks support.
    if original_state == '设备不支持' and not result.get('error') and not params:
        original_state = '未返回'
        result['state'] = original_state
    hard_error = original_state in {
        '读取失败', '查询失败', '接口不可用', '权限不足', '设备不支持'}
    if hard_error:
        result.update(health='未确认（本次读取未成功）', temperature=None, temperature_c=None,
                      temperature_max_c=None, power_on_hours=None,
                      interpretation='本次未成功读取该磁盘的SMART指标，不能据此判断正常；原始参数仅供核对。')
    elif is_ssd is False:
        result.update(metric='不适用（非固态）', remaining_display='不适用', used_display='不适用',
                      interpretation='非固态磁盘不按闪存写入寿命计算；SMART健康等其他指标仍保留。')
    elif candidates:
        # Prefer one coherent metric; never silently combine contradictory pairs.
        candidates.sort(key=lambda item: item[0], reverse=True)
        priority, value, row, raw = candidates[0]
        remaining = value if priority == 2 else max(0.0, 100.0 - value)
        used = 100.0 - value if priority == 2 else value
        name = '介质剩余寿命（MWI）' if priority == 2 else '额定耐久已用（Percentage Used）'
        result.update(metric=name, raw_value=raw, raw_value_display=_display(value),
                      remaining_pct=remaining, used_pct=used,
                      remaining_display=_display(remaining) + '%' + ('（换算）' if priority == 1 else ''),
                      used_display=_display(used) + '%' + ('（换算）' if priority == 2 else ''),
                      remaining_origin='reported' if priority == 2 else 'derived',
                      used_origin='derived' if priority == 2 else 'reported',
                      threshold=number(row.get('raw_threshold', row.get('threshold'))),
                      worst=number(row.get('raw_worst', row.get('worst'))), state='已获取')
        if original_state == '值无效':
            # The old parser incorrectly rejected legitimate Percentage Used >100.
            result['error'] = ''
        result['interpretation'] = ('MWI为VMware返回的剩余寿命指标；已用率=100−MWI。' if priority == 2 else
                                    'Percentage Used为已消耗的额定写入耐久；剩余率=max(0,100−已用率)。')
        if priority == 1 and value > 100:
            result['interpretation'] += '已超过额定耐久估算，不能据此单独判断设备已经故障。'
        if priority == 1 and value == 255:
            result['used_display'] = '≥255%'
            result['interpretation'] += '接口值255代表已用率超过254%，不是精确测量的255%。'
        if any(abs((v if p == 2 else max(0, 100-v)) - remaining) > 0.1 for p, v, _, _ in candidates[1:]):
            result['interpretation'] += '同时返回的其他耐久指标不一致，当前两率仅按此主指标计算，请核对原始参数。'
        if is_ssd is None:
            result['interpretation'] += '介质类型仍由资产接口确认，不能仅据此字段推定SSD。'
    else:
        if original_state in {'不适用', '设备不支持'} and not result.get('error'):
            original_state = '未返回'
        if original_state == '不适用' and is_ssd is None:
            original_state = '未返回'
            if result.get('error') == '非 SSD/NVMe':
                result['error'] = ''
        if invalid:
            result.update(state='值无效', interpretation='耐久值格式或范围无效；原始参数保留供核对。')
        elif recognized:
            result.update(state='指标值未返回', interpretation='接口列出了耐久参数，但没有提供可用数值。')
        elif params:
            result.update(state='已获取但无耐久指标', interpretation='本次有SMART数据，但没有受支持的寿命指标。')
        else:
            result.update(state=original_state, interpretation='本次接口未返回寿命数据；请结合采集状态及设备支持情况核对。')
        result['metric'] = '未提供可用寿命指标'
    if len(health_values) > 1 and not hard_error:
        result['interpretation'] += '同次返回的SMART健康值相互冲突，健康状态暂不能确认。'
    return result


def smart_row(disk, smart):
    result = interpret_smart(smart, disk.get('is_ssd'))
    result.update(media_description=media_label(disk), capacity_display=capacity_label(disk))
    return result
