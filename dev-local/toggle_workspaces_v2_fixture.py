"""Enable/disable only the exact owned synthetic UI workspace fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID
import psycopg

if os.getenv('WSL_DISTRO_NAME') != 'MegmeetDev' or sys.argv[1:] not in (['enable'], ['disable']):
    raise SystemExit('LOCAL_FIXTURE_SCOPE_REQUIRED')
fixture = json.loads((Path(__file__).parent / 'evidence/workspaces-v2-fixture.json').read_text())
workspace = str(UUID(fixture['workspace_id']))
ip = subprocess.check_output(['docker', 'inspect', '--format',
    '{{(index .NetworkSettings.Networks "megmeet-dev-local_local-only").IPAddress}}',
    'megmeet-dev-local-postgres-1'], text=True).strip()
with psycopg.connect(f'postgresql://megmeet_dev:local-synthetic-database-only@{ip}:5432/megmeet_dev') as conn:
    row = conn.execute('''UPDATE platform.hybrid_workspaces SET enabled=%s
        WHERE id=%s AND kind='group' AND organization_code=%s RETURNING id''',
        (sys.argv[1] == 'enable', workspace, 'TEST-GROUP-' + workspace)).fetchone()
    if not row:
        raise RuntimeError('OWNED_FIXTURE_MISMATCH')
print('OWNED_V2_FIXTURE_' + sys.argv[1].upper() + ': PASS')
