#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

python3 - "$HERE" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
manifest=json.loads((root/'manifest.json').read_text())
assert manifest['release']=='megmeet-personal-pilot-r1-20260916'
for name, expected in manifest['files'].items():
    assert name==Path(name).name
    path=root/name
    assert path.is_file() and not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected
print('PERSONAL_PILOT_PACKAGE: PASS')
PY

# Interactive only for the two employee numbers; the long-running work follows
# in the background so the bastion session is not tied to startup time.
python3 -B "$HERE/pilot_deploy.py" prepare

LOG=$(mktemp /tmp/megmeet-personal-pilot.XXXXXX.log)
chmod 600 "$LOG"
python3 - "$LOG" <<'PY'
import os,stat,sys
path='/tmp/megmeet-personal-pilot-current-log.txt'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
try:
    s=os.fstat(fd)
    assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1
    os.fchmod(fd,0o600)
    os.ftruncate(fd,0)
    os.write(fd,(sys.argv[1]+'\n').encode())
finally:
    os.close(fd)
PY
nohup bash -c '
set +e
python3 -B /data/megmeet-ai-platform-new/personal-entry-pilot/app/pilot_deploy.py activate
rc=$?
printf "\nPERSONAL_PILOT_DEPLOY_EXIT:%s\n" "$rc"
exit "$rc"
' >"$LOG" 2>&1 < /dev/null &
printf 'PERSONAL_PILOT_PID: %s\n' "$!"
printf 'PERSONAL_PILOT_LOG: %s\n' "$LOG"
printf 'PERSONAL_PILOT: STARTED\n'
