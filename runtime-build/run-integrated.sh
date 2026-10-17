set -euo pipefail
ROOT=/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime
python3 - "$ROOT" <<'PY'
import json,subprocess,sys
from pathlib import Path
r=Path(sys.argv[1])
images={}
for name,tag in [('gateway','maimi-r26-gateway-candidate:20260928'),('worker','maimi-dsh020-r41:local')]:
 image=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',tag],text=True).strip()
 assert image.startswith('sha256:') and len(image)==71
 images[name]={'tag':tag,'raw_id':image}
(r/'tests/local-images.json').write_text(json.dumps(images,indent=2)+'\n')
PY
python3 "$ROOT/tests/test-integrated-native.py"
