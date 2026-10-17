"""Small JSON-only result contract, shared by cloud and desktop conversation UI."""
from __future__ import annotations

import math
import re

SCHEMA = 'megmeet-project-result-v1'
KEYS = ('meeting', 'employee-cards', 'infrastructure-monitor', 'heatcost', 'service')
NAMES = dict(zip(KEYS, ('会议文档', '工卡制作', 'vmware', '图纸成本分析', 'service')))
OPAQUE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def text(value, maximum=2000):
    """Render as plain text only; frontend must use textContent, never innerHTML."""
    return str(value or '').replace('\x00', '')[:maximum]


def cell(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return text(value, 500)


def table(title, columns, rows, *, maximum=100):
    keys = [c['key'] for c in columns]
    return {'type': 'table', 'title': text(title, 100), 'columns': columns,
            'rows': [{k: cell(r.get(k)) for k in keys} for r in rows[:maximum]],
            'total_rows': len(rows), 'truncated': len(rows) > maximum}


def notice(message, level='info'):
    return {'type': 'notice', 'level': level, 'text': text(message)}


def card(key, result_id, status, summary, *, sections=(), artifacts=(), actions=()):
    if key not in KEYS or not OPAQUE.fullmatch(result_id):
        raise ValueError('invalid result identity')
    if status not in {'needs_input', 'running', 'completed', 'failed'}:
        raise ValueError('invalid result status')
    return {'schema': SCHEMA, 'id': result_id, 'connector': key, 'title': NAMES[key],
            'status': status, 'summary': text(summary), 'sections': list(sections),
            'artifacts': list(artifacts), 'actions': list(actions)}


def invoke_action(label, connector, action, arguments):
    return {'id': action, 'label': label, 'kind': 'invoke', 'connector': connector,
            'action': action, 'arguments': arguments}
