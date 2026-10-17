#!/usr/bin/env bash
set -euo pipefail
umask 077
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

python3 -B - "$HERE" <<'PY'
import hashlib,json,os,stat,sys
from pathlib import Path
root=Path(sys.argv[1])
assert os.geteuid()==0
manifest=json.loads((root/'manifest.json').read_text())
assert set(manifest)=={'release','files'}
assert manifest['release']=='megmeet-native-entry-login-r1-20260916'
assert set(manifest['files'])=={'entry_app.py','entry_login.py','login_deploy.py','start-login.sh','LOGIN-README.md'}
for name,expected in manifest['files'].items():
    path=root/name
    for ancestor in (path,*path.parents):
        assert not ancestor.is_symlink()
    info=path.stat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected
print('LOGIN_ENTRY_PACKAGE: PASS')
PY

LOG=$(mktemp /tmp/megmeet-native-login.XXXXXX.log)
chmod 600 "$LOG"
python3 - "$LOG" <<'PY'
import os,stat,sys
path='/tmp/megmeet-native-login-current-log.txt'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
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
python3 -B "$1/login_deploy.py"
rc=$?
printf "\nLOGIN_ENTRY_UPGRADE_EXIT:%s\n" "$rc"
exit "$rc"
' _ "$HERE" >"$LOG" 2>&1 < /dev/null &
printf 'LOGIN_ENTRY_UPGRADE_PID: %s\n' "$!"
printf 'LOGIN_ENTRY_UPGRADE_LOG: %s\n' "$LOG"
printf 'LOGIN_ENTRY_UPGRADE: STARTED\n'
