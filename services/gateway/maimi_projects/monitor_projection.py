"""Explicit projections of original tonggong disk-report fields; no raw logs."""
from .contracts import text
from .disk_semantics import smart_row
from .disk_alerts import endurance_alert, load_policy

SMART_FIELDS = [('host', '主机'), ('device', '设备'), ('media_description', '介质类型'),
                ('capacity_display', '磁盘容量'), ('state', '采集状态'), ('metric', '耐久指标'),
                ('raw_value_display', '接口原始值'), ('remaining_display', '寿命剩余'), ('used_display', '寿命已用'),
                ('health', 'SMART健康'), ('temperature_c', '温度℃'), ('power_on_hours', '通电小时'),
                ('interpretation', '指标说明'), ('source', '来源'), ('error', '采集说明'), ('association', '关联状态')]
SMART_DETAIL_COLUMNS = [('asset', '磁盘'), ('media_description', '介质类型'), ('capacity_display', '磁盘容量'),
                        ('metric', '耐久指标'), ('raw_value_display', '接口原始值'),
                        ('source', 'SMART来源'), ('temperature_c', '温度℃'),
                        ('power_on_hours', '通电小时'), ('vsan_health_display', 'vSAN状态（缓存）'),
                        ('field_sources_display', '资产字段来源'), ('error', 'SMART采集说明')]

DISK_SECTIONS = (
    ('disks', '物理磁盘', [('host', '主机'), ('device', '设备'), ('media', '介质'), ('model', '型号'), ('serial', '序列号'),
                       ('capacity_gb', '容量GiB'), ('endurance_display', '耐久度'), ('endurance_alert', '耐久报警'), ('status', '状态'),
                       ('local', '本地盘'), ('location', '位置'), ('source', '来源')]),
    ('smart_health', 'SMART与磁盘总览', [('asset', '磁盘'), ('media_description', '介质'), ('capacity_display', '容量GiB'),
                                       ('health_summary', '健康'), ('remaining_display', '寿命剩余'),
                                       ('used_display', '寿命已用'), ('endurance_alert', '耐久报警'),
                                       ('endurance_action', '建议处理'), ('state', '采集状态')]),
    ('smart_details', 'SMART指标与来源', SMART_DETAIL_COLUMNS),
    ('smart_parameters', 'SMART原始参数', [('host', '主机'), ('device', '设备'), ('parameter', '参数'), ('value', '原始值'),
                                        ('threshold', '阈值'), ('worst', '最差'), ('association', '关联状态')]),
    ('hosts', '主机磁盘汇总', [('host', '主机'), ('disk_total', '磁盘'), ('ssd', 'SSD'), ('hdd', 'HDD'), ('unknown', '未知介质'),
                            ('total_cap_gb', '容量GiB'), ('state', '路径状态'), ('luns', 'LUN'), ('paths', '路径'), ('active', '活动'),
                            ('standby', '备用'), ('bad', '故障'), ('unknown_paths', '未知路径')]),
    ('multipath_issues', '多路径问题', [('host', '主机'), ('device', '设备'), ('policy', '策略'), ('path_total', '路径总数'),
                                     ('bad_paths', '故障路径'), ('unknown_paths', '未知路径')]),
    ('datastores', '数据存储', [('datastore', '名称'), ('hosts', '主机'), ('cap_gb', '容量GiB'), ('free_gb', '空闲GiB'), ('used_pct', '已用%')]),
    ('smart_unmatched', '未匹配SMART记录', [('asset', '磁盘标识（未关联）'), ('media_description', '介质'),
                                        ('state', '采集状态'), ('metric', '耐久指标'), ('raw_value_display', '接口原始值'),
                                        ('remaining_display', '寿命剩余'), ('used_display', '寿命已用'),
                                        ('interpretation', '指标说明'), ('source', '来源'), ('error', '采集说明')]),
    ('collection_errors', '采集错误', [('scope', '范围'), ('host', '主机'), ('message', '说明')]),
)


def scalar(value):
    return value if value is None or isinstance(value, (str, bool, int, float)) else None


def joined(value):
    return text('、'.join(v for v in value[:100] if isinstance(v, str)), 500) if isinstance(value, list) else scalar(value)


def entries(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > 100000 or any(not isinstance(v, dict) for v in value):
        raise ValueError('monitor_response_invalid')
    return value


def endurance_label(interpreted):
    """Same disk, same interpreted metric; no join or capacity-based estimate."""
    if interpreted.get('remaining_pct') is not None:
        label = '剩余' + interpreted['remaining_display'] + '；已用' + interpreted['used_display']
        if '不一致' in interpreted.get('interpretation', ''):
            label += '；指标不一致，请核对原始参数'
        return label
    if interpreted.get('remaining_display') == '不适用':
        return '不适用（非固态）'
    state = text(interpreted.get('state'), 80)
    return '未返回' + ('（' + state + '）' if state not in ('', '未返回', '已获取') else '')


def vsan_label(vsan):
    """Fold only complete, consistent cached OK; keep all other evidence."""
    def normalized(key):
        value = vsan.get(key)
        return value.strip().lower() if isinstance(value, str) else ''
    op, summary, description = (normalized(key) for key in ('operational_health', 'summary_health', 'description'))
    if (op, summary, description) == ('green', 'green', 'ok') and not vsan.get('error'):
        return '正常'
    colors = {'green': '正常', 'yellow': '警告', 'red': '异常', 'unknown': '未知', 'gray': '未知'}
    parts = []
    for key, label in [('operational_health', '运行'), ('summary_health', '概要'), ('description', '说明'), ('error', '采集说明')]:
        value = vsan.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            translated = colors.get(normalized(key)) if key in ('operational_health', 'summary_health') else None
            if key == 'description' and normalized(key) == 'ok':
                translated = '正常'
            parts.append(label + '：' + (translated or text(value, 100)))
    if vsan.get('error'):
        status = '未确认（读取异常）'
    elif 'red' in (op, summary):
        status = '异常'
    elif 'yellow' in (op, summary):
        status = '警告'
    else:
        status = '未完全确认' if parts else '未返回'
    return status + ('；' + '；'.join(parts) if parts else '')


def project_disk(data, alert_policy=None):
    alert_policy = load_policy() if alert_policy is None else alert_policy
    projected = {key: [] for key, _, _ in DISK_SECTIONS}
    for key in ('disks', 'hosts', 'multipath_issues', 'datastores', 'collection_errors'):
        cols = dict((name, columns) for name, _, columns in DISK_SECTIONS)[key]
        for entry in entries(data, key):
            projected[key].append({k: scalar(entry.get(k)) for k, _ in cols})
    for original, row in zip(entries(data, 'hosts'), projected['hosts']):
        paths = original.get('mpath') if isinstance(original.get('mpath'), dict) else {}
        row.update({key: scalar(paths.get(key)) for key in ('state', 'luns', 'paths', 'active', 'standby', 'bad')})
        row['unknown_paths'] = scalar(paths.get('unknown'))
    for original, row in zip(entries(data, 'multipath_issues'), projected['multipath_issues']):
        row.update({key: joined(original.get(key)) for key in ('bad_paths', 'unknown_paths')})
    for original, row in zip(entries(data, 'datastores'), projected['datastores']):
        row['hosts'] = joined(original.get('hosts'))
    smart_keys = [key for key, _ in SMART_FIELDS]
    for source_key, association in [('disks', '已关联磁盘'), ('smart_unmatched', '未匹配磁盘')]:
        for index, original in enumerate(entries(data, source_key)):
            smart = original.get('smart') if source_key == 'disks' else original
            smart = smart if isinstance(smart, dict) else {}
            # Unassociated SMART records have no trusted physical-disk media or
            # capacity. Interpret their counters without inferring an SSD.
            interpreted = smart_row(original if source_key == 'disks' else {}, smart)
            row = {k: scalar(interpreted.get(k)) for k in smart_keys}
            row.update(host=scalar(original.get('host')), device=scalar(original.get('device')), association=association)
            row['asset'] = text(row.get('host'), 180) + ' / ' + text(row.get('device'), 180)
            if source_key == 'disks':
                alert = endurance_alert(original, alert_policy)
                row['endurance_alert'] = alert['label']
                row['endurance_action'] = alert['action']
                row['endurance_level'] = alert['level']
                row['read_only_observed'] = '设备已报告只读' if alert['read_only'] is True else ('未报告只读' if alert['read_only'] is False else '未返回只读状态')
                projected['disks'][index]['endurance_display'] = endurance_label(interpreted)
                projected['disks'][index]['endurance_alert'] = alert['label']
                # The explanatory column is hidden, but conflicting metrics
                # must still be visible in the collection note.
                if '不一致' in interpreted.get('interpretation', ''):
                    row['error'] = (text(row.get('error'), 300) + '；' if row.get('error') else '') + '耐久指标不一致，请核对SMART原始参数。'
                vsan = original.get('vsan_health') if isinstance(original.get('vsan_health'), dict) else {}
                row.update(vsan_health_display=vsan_label(vsan),
                           vsan_observed_at=scalar(vsan.get('observed_at')) or '未返回',
                           vsan_source=scalar(vsan.get('source')) or '未返回')
                row['health_summary'] = 'SMART：' + text(row.get('health'), 80)
                if row['vsan_health_display'] != '未返回':
                    row['health_summary'] += '；vSAN：' + row['vsan_health_display'] + '（缓存）'
                origins = original.get('field_sources') if isinstance(original.get('field_sources'), dict) else {}
                row['field_sources_display'] = '；'.join(label + '：' + text(origins[key], 100) for key, label in
                    [('capacity_gb', '容量'), ('is_ssd', '固态标志'), ('media', '介质'), ('model', '型号'), ('serial', '序列号')]
                    if isinstance(origins.get(key), str)) or '原资产接口（未单列字段来源）'
                projected['smart_health'].append(row)
                projected['smart_details'].append(row)
            else:
                row['interpretation'] = '未关联物理磁盘，介质与容量未知；以下仅解释本条SMART返回。' + text(row.get('interpretation'), 420)
                projected['smart_unmatched'].append(row)
            params = smart.get('raw_params', [])
            if not isinstance(params, list) or len(params) > 10000:
                raise ValueError('monitor_response_invalid')
            for param in params:
                if not isinstance(param, dict):
                    raise ValueError('monitor_response_invalid')
                projected['smart_parameters'].append({
                    'host': scalar(original.get('host')), 'device': scalar(original.get('device')), 'association': association,
                    'parameter': scalar(param.get('parameter')), 'value': scalar(param.get('raw_value', param.get('value'))),
                    'threshold': scalar(param.get('raw_threshold', param.get('threshold'))),
                    'worst': scalar(param.get('raw_worst', param.get('worst')))})
                if len(projected['smart_parameters']) > 100000:
                    raise ValueError('monitor_response_invalid')
    return projected
