#!/usr/bin/env bash
# Run only the isolated personal-entry candidate check. No live activation.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
test "$(id -u)" = 0 || { echo 'PRECHECK: root required for Docker'; exit 1; }
command -v python3 >/dev/null
command -v docker >/dev/null

python3 - "$HERE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
required = {'entry_app.py', 'host_launcher.py', 'probe.py', 'probe_client.py', 'probe_page.py', 'test_host_launcher.py', 'start.sh'}
if not required.issubset(manifest):
    raise SystemExit('PACKAGE_CHECK: missing entries')
for name, expected in manifest.items():
    target = root / name
    if target.is_symlink() or not target.is_file() or target.resolve().parent != root:
        raise SystemExit('PACKAGE_CHECK: invalid file')
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise SystemExit('PACKAGE_CHECK: checksum mismatch')
print('PERSONAL_ENTRY_PACKAGE: PASS')
PY

# mktemp directories default to 700. Only this verified code package is public
# to UID10001 inside its read-only container mount; no credentials live here.
chmod 755 "$HERE"
chmod 644 "$HERE/entry_app.py" "$HERE/host_launcher.py" "$HERE/probe.py" "$HERE/probe_client.py" "$HERE/probe_page.py" "$HERE/test_host_launcher.py"
LOG=$(mktemp /tmp/megmeet-native-personal-entry.XXXXXX.log)
POINTER=$(mktemp /tmp/.megmeet-native-personal-entry-pointer.XXXXXX)
printf '%s\n' "$LOG" > "$POINTER"
mv -T -- "$POINTER" /tmp/megmeet-native-personal-entry-current-log.txt
nohup python3 -u -B "$HERE/probe.py" > "$LOG" 2>&1 < /dev/null &
printf 'PERSONAL_ENTRY_PROBE_PID: %s\n' "$!"
printf 'PERSONAL_ENTRY_PROBE_LOG: %s\n' "$LOG"
echo 'PERSONAL_ENTRY_PROBE: STARTED'
