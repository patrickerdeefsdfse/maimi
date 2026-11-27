import hashlib
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = HERE
manifest = json.loads((src/'manifest.json').read_bytes())
destination = Path.cwd()/f"{manifest['plugin_key']}-{manifest['version']}.zip"
with zipfile.ZipFile(destination, 'x') as out:
    for name in ['manifest.json','SKILL.md']:
        entry = zipfile.ZipInfo(name, (2020,1,1,0,0,0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o100644 << 16
        out.writestr(entry,(src/name).read_bytes().replace(b'\r\n',b'\n'))
print(destination.name, hashlib.sha256(destination.read_bytes()).hexdigest())
