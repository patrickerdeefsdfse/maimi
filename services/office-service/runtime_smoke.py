"""Run only in a disposable, network-none Office image with synthetic files.

Exercises the new resource bootstrap against actual LibreOffice/OCR packages;
does not import FastAPI or need a newly built service image.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

assert os.environ.get('OFFICE_RUNTIME_SMOKE') == '1'
ENGINE = Path('/opt/office/engine.py')
RUNNER = Path('/tests/runner.py')
checks = []

with tempfile.TemporaryDirectory(prefix='shared-office-runtime-') as temporary:
    root = Path(temporary)
    scratch = root / '.office-tmp'
    scratch.mkdir()
    env = {'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'HOME': str(scratch), 'TMPDIR': str(scratch), 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1', 'OMP_NUM_THREADS': '1',
           'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
           'OFFICE_CHILD_MEMORY_BYTES': str(3 * 1024**3), 'OFFICE_CHILD_CPU_SECONDS': '125'}

    def call(label, operation, **kwargs):
        request = {'protocol': 'megmeet-office-v1', 'workspace': str(root),
                   'operation': operation, **kwargs}
        result = subprocess.run([sys.executable, '-I', '-B', str(RUNNER), str(ENGINE)],
            input=json.dumps(request).encode(), capture_output=True, env=env, cwd=scratch, timeout=150)
        assert result.returncode == 0, label + ': engine process failed'
        response = json.loads(result.stdout)
        assert response['ok'], label + ': ' + response['error']['code']
        value = response['result']
        if kwargs.get('output'):
            output = root / kwargs['output']
            assert output.is_file() and output.stat().st_size == value['bytes']
            assert hashlib.sha256(output.read_bytes()).hexdigest() == value['sha256']
        checks.append(label)
        return value

    call('docx_create', 'create', output='source.docx', options={'paragraphs': ['Office server review 123', '共享服务器测试']})
    call('docx_edit', 'edit', input='source.docx', output='edited.docx', options={'replacements': [{'find': '123', 'replace': '456'}]})
    word = call('docx_read', 'read', input='edited.docx', options={})
    assert '456' in word['text']
    call('libreoffice_docx_pdf', 'convert', input='edited.docx', output='word.pdf', options={})
    call('pdf_extract', 'pdf', input='word.pdf', output='page.pdf', options={'action': 'extract', 'pages': [1]})
    call('pdf_read', 'read', input='page.pdf', options={})
    call('xlsx_create', 'create', output='table.xlsx', options={'sheets': [{'name': 'Test', 'rows': [['A', 'B'], [2, 3]], 'cells': [{'cell': 'C2', 'formula': '=SUM(A2:B2)'}]}]})
    call('libreoffice_xlsx_recalculate', 'convert', input='table.xlsx', output='calculated.xlsx', options={})
    call('pptx_create', 'create', output='slides.pptx', options={'slides': [{'title': 'Server test', 'bullets': ['Synthetic only']} ]})
    call('pptx_inspect', 'inspect', input='slides.pptx', options={})
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (900, 160), 'white')
    painter = ImageDraw.Draw(image)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 64)
    painter.text((20, 35), 'OFFICE 12345', font=font, fill='black')
    image.save(root / 'source.png')
    call('image_transform', 'image', input='source.png', output='resized.png', options={'width': 800})
    ocr = call('offline_ocr', 'ocr', input='source.png', options={})
    assert '12345' in ocr['text'], 'OCR did not read synthetic digits'

print(json.dumps({'status': 'PASS', 'checks': checks, 'default_child_memory_bytes': 3 * 1024**3,
                  'network': 'none', 'real_company_data': False}))
