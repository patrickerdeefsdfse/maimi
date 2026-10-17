#!/usr/bin/env python3
"""Validate fixed offline image tags against the release's platform image IDs."""
import json
from pathlib import Path
import re
import subprocess
import sys
from configure import docker_env, regular_path

SOURCES = {
    'megmeet-ai-gateway:sso-20260921-r11': None,
    'megmeet-personal-cloud-manager:20260921-r11': None,
    'megmeet-personal-dsh:20260921-r11': None,
    'megmeet-desktop-postgres:16-20260918': 'sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94',
    'megmeet-desktop-redis:7-20260918': 'sha256:71da9275c5f3fcb97d0fa0c8c5b36cc995327265420f17a04bfd544f458059f7',
}


def verify(manifest):
    path = Path(manifest).absolute()
    regular_path(path, exists=True)
    if path.stat().st_size > 65536:
        raise ValueError('镜像发布清单过大')
    data = json.loads(path.read_text(encoding='utf-8'))
    rows = data.get('images') if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) != len(SOURCES):
        raise ValueError('镜像发布清单不完整')
    records = {row['reference']: row for row in rows if isinstance(row, dict) and isinstance(row.get('reference'), str)}
    if set(records) != set(SOURCES):
        raise ValueError('镜像发布清单包含缺失、重复或未知引用')
    for reference, source_digest in SOURCES.items():
        record = records[reference]
        identifier = record.get('platform_image_id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', identifier):
            raise ValueError('镜像发布清单缺少平台镜像ID')
        recorded_source = record.get('source_digest')
        if source_digest and (not isinstance(recorded_source, str) or recorded_source.rsplit('@', 1)[-1] != source_digest):
            raise ValueError('基础镜像来源与固定版本不符')
        inspected = subprocess.run(['docker', 'image', 'inspect', '--platform', 'linux/amd64', reference,
                                    '--format', '{{.Id}} {{.Os}}/{{.Architecture}}'],
                                   capture_output=True, text=True, env=docker_env())
        if inspected.returncode or inspected.stdout.strip() != identifier + ' linux/amd64':
            raise ValueError('离线镜像缺失、内容或平台不符；未启动服务')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2: raise ValueError('Usage: python3 verify-images.py /path/images-manifest.json')
        verify(sys.argv[1])
        print('OFFLINE_IMAGE_IDENTITIES: PASS (5 images, linux/amd64)')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('OFFLINE_IMAGE_CHECK: STOP (' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__) + ')', file=sys.stderr)
        raise SystemExit(1)
