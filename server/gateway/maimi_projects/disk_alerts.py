"""Read-only SSD observations. Policy thresholds do not predict a failure date."""
from .disk_semantics import interpret_smart, metric_key, number

DEFAULT_POLICY = {'warning_used_pct': 80, 'critical_used_pct': 90}
SEVERITY = {'not_applicable': -1, 'unknown': 0, 'normal': 1, 'warning': 2, 'critical': 3}


def validate_policy(policy=None):
    policy = DEFAULT_POLICY if policy is None else policy
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY):
        raise ValueError('ssd_alert_policy_invalid')
    warning, critical = (number(policy[k]) for k in DEFAULT_POLICY)
    if warning is None or critical is None or not 1 <= warning < critical <= 100:
        raise ValueError('ssd_alert_policy_invalid')
    return {'warning_used_pct': warning, 'critical_used_pct': critical}


def _byte(value):
    if isinstance(value, str) and value.lower().startswith('0x'):
        try: value = int(value, 16)
        except ValueError: return None
    value = number(value)
    return int(value) if value is not None and value.is_integer() and 0 <= value <= 255 else None


def endurance_alert(disk, policy=None):
    policy = validate_policy(policy)
    smart = disk.get('smart') if isinstance(disk.get('smart'), dict) else {}
    interpreted = interpret_smart(smart, disk.get('is_ssd'))
    base = {'level': 'unknown', 'label': '无法评估', 'code': 'SSD_ENDURANCE_UNKNOWN',
            'message': '本次没有可用的固态耐久数据，不能据此判断正常。',
            'action': '核对采集权限、设备支持和厂商硬件管理平台。',
            'used_pct': interpreted.get('used_pct'), 'remaining_pct': interpreted.get('remaining_pct'),
            'read_only': None, 'warning_used_pct': policy['warning_used_pct'],
            'critical_used_pct': policy['critical_used_pct']}
    if disk.get('is_ssd') is False:
        return {**base, 'level': 'not_applicable', 'label': '不适用', 'code': 'NON_SSD',
                'message': '机械盘/非SSD不按闪存耐久百分比报警。', 'action': '查看SMART健康与设备告警。'}
    if disk.get('is_ssd') is not True:
        return {**base, 'message': '介质类型未确认，暂不把耐久参数解释成固态硬盘健康。'}
    # Fail closed for stale/error records: raw parameters are retained for audit,
    # but cannot prove the current read-only state after a failed collection.
    if interpreted.get('state') in {'读取失败', '查询失败', '接口不可用', '权限不足', '设备不支持'}:
        return base
    params = smart.get('raw_params', [])
    warnings = []
    other_nvme_warning = False
    for row in params if isinstance(params, list) else []:
        if not isinstance(row, dict): continue
        key = metric_key(row.get('parameter'))
        # Only explicitly NVMe-identified bits have the NVMe bit semantics.
        if key in {'nvmecriticalwarning', 'criticalwarning'} and (disk.get('media') == 'NVMe' or key == 'nvmecriticalwarning'):
            parsed = _byte(row.get('raw_value', row.get('value')))
            if parsed is not None: warnings.append(parsed)
    if warnings and len(set(warnings)) == 1:
        bits = warnings[0]
        base['read_only'] = bool(bits & 8)
        if bits & 8:
            return {**base, 'level': 'critical', 'label': '紧急：设备已只读', 'code': 'NVME_MEDIA_READ_ONLY',
                    'message': 'NVMe设备明确报告介质已进入只读模式；该结论来自设备信号，不是耐久百分比推测。',
                    'action': '立即通知存储管理员，核对备份并安排迁移/更换；不要继续向该设备安排写入。'}
        if bits & 4:
            return {**base, 'level': 'critical', 'label': '紧急：可靠性下降', 'code': 'NVME_RELIABILITY_DEGRADED',
                    'message': '设备报告介质/内部错误导致可靠性下降。', 'action': '尽快核对备份、评估迁移并联系硬件厂商。'}
        if bits & 1:
            return {**base, 'level': 'critical', 'label': '紧急：备用容量不足', 'code': 'NVME_SPARE_BELOW_THRESHOLD',
                    'message': '设备报告可用备用容量已低于设备自身阈值。', 'action': '尽快核对备份并安排硬件检修/更换。'}
        other_nvme_warning = bool(bits)
    elif warnings:
        return {**base, 'message': '同次只读/健康标志相互冲突，不能确认当前设备状态。'}
    if interpreted.get('health') == '即将故障':
        return {**base, 'level': 'critical', 'label': '紧急：SMART故障预警', 'code': 'SMART_IMPENDING_FAILURE',
                'message': '设备SMART明确报告即将故障。', 'action': '尽快联系存储管理员核对备份并安排更换。'}
    if '不一致' in interpreted.get('interpretation', ''):
        return {**base, 'message': '耐久指标相互冲突，请核对原始参数；暂不按单个百分比给出正常结论。'}
    used = interpreted.get('used_pct')
    if used is None:
        if other_nvme_warning:
            return {**base, 'level': 'warning', 'label': '设备健康告警', 'code': 'NVME_CRITICAL_WARNING',
                    'message': 'NVMe设备报告健康警告；耐久数值未返回。', 'action': '查看硬件管理平台和SMART原始参数。'}
        if interpreted.get('health') == '警告':
            return {**base, 'level': 'warning', 'label': 'SMART健康警告', 'code': 'SMART_HEALTH_WARNING',
                    'message': '设备SMART报告健康警告；耐久数值未返回。', 'action': '核对硬件管理平台、备份和SMART原始参数。'}
        return base
    if used >= 100:
        return {**base, 'level': 'critical', 'label': '额定耐久已耗尽', 'code': 'SSD_RATED_ENDURANCE_CONSUMED',
                'message': '已用达到/超过100%：估计的额定写入耐久已消耗；不等于自动只读或已经故障。',
                'action': '优先安排迁移/更换，结合设备健康、厂商建议与备份情况处理。'}
    if used >= policy['critical_used_pct']:
        return {**base, 'level': 'critical', 'label': '耐久严重预警', 'code': 'SSD_ENDURANCE_CRITICAL',
                'message': '已用达到配置的严重预警线，写入寿命余量偏低。',
                'action': '尽快计划更换；若伴随高延迟、I/O错误或健康告警，及时升级处理。'}
    if other_nvme_warning:
        return {**base, 'level': 'warning', 'label': '设备健康告警', 'code': 'NVME_CRITICAL_WARNING',
                'message': 'NVMe设备报告健康警告，请核对温度/备份电源等原始状态。', 'action': '查看硬件管理平台和SMART原始参数。'}
    if used >= policy['warning_used_pct']:
        return {**base, 'level': 'warning', 'label': '耐久预警', 'code': 'SSD_ENDURANCE_WARNING',
                'message': '已用达到配置的预警线。', 'action': '检查备份与写入负载，纳入更换计划。'}
    if interpreted.get('health') == '警告':
        return {**base, 'level': 'warning', 'label': 'SMART健康警告', 'code': 'SMART_HEALTH_WARNING',
                'message': '设备SMART报告健康警告，不能因耐久值低于阈值而忽略。', 'action': '核对硬件管理平台、备份和SMART原始参数。'}
    return {**base, 'level': 'normal', 'label': '未触发耐久阈值', 'code': 'SSD_ENDURANCE_BELOW_THRESHOLD',
            'message': '当前耐久值未达到配置的预警线；并不保证设备不会因其他原因故障。',
            'action': '继续监测设备健康与备份。'}


def load_policy(path=None):
    import json
    from pathlib import Path
    path = Path(path or '/etc/tonggong/ssd-alerts.json')
    if not path.exists(): return dict(DEFAULT_POLICY)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('ssd_alert_policy_invalid')
    return validate_policy(json.loads(path.read_text(encoding='utf-8')))


def annotate_report(report, policy=None):
    policy = load_policy() if policy is None else validate_policy(policy)
    report = dict(report)
    report['disks'] = [{**disk, 'ssd_alert': endurance_alert(disk, policy)} for disk in report.get('disks', [])]
    return report
