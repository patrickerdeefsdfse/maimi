"""Download upstream OCR weights at build time and freeze them for offline use."""
import hashlib
import json
from pathlib import Path
import shutil
from rapidocr import RapidOCR

ROOT = Path(__file__).resolve().parent
models = ROOT / 'ocr-models'
models.mkdir(exist_ok=True)
engine = RapidOCR()
import rapidocr
installed = Path(rapidocr.__file__).parent
paths = list(installed.rglob('*.onnx'))
required = {}
for role in ('det', 'rec', 'cls'):
    candidates = [p for p in paths if ('_' + role + '_') in p.name]
    if len(candidates) != 1:
        raise SystemExit('Unexpected OCR model selection: ' + role)
    source = candidates[0]
    shutil.copyfile(source, models / source.name)
    required[role.capitalize() + '.model_path'] = source.name
(ROOT / 'ocr-config.json').write_text(json.dumps(required, indent=2) + '\n')
manifest = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in models.glob('*.onnx')}
(ROOT / 'ocr-models.json').write_text(json.dumps({'upstream':'https://github.com/RapidAI/RapidOCR', 'models':manifest}, indent=2)+'\n')
print('OFFLINE_OCR_MODELS:', len(manifest))
