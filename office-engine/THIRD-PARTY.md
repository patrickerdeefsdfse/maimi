# MEGMEET Office component 1.0.0

Independent JSON protocol: megmeet-office-v1. DSH integration is a replaceable
Cordis tool adapter; the document engine imports no DSH packages.

Primary projects, preserved licenses and source:

- Microsoft MarkItDown (MIT): https://github.com/microsoft/markitdown
- python-docx / python-pptx (MIT): https://github.com/python-openxml
- openpyxl (MIT): https://openpyxl.readthedocs.io/
- pypdf (BSD-3-Clause): https://github.com/py-pdf/pypdf
- PDFium/pypdfium2 (Apache/BSD; bundled license notices): https://github.com/pypdfium2-team/pypdfium2
- RapidOCR (Apache-2.0), frozen OCR weights: https://github.com/RapidAI/RapidOCR
- ONNX Runtime (MIT): https://github.com/microsoft/onnxruntime
- Pillow (HPND): https://github.com/python-pillow/Pillow
- ReportLab (BSD): https://www.reportlab.com/
- LibreOffice (MPL-2.0 and bundled license notices): https://www.libreoffice.org/
  Windows 26.2.6 source: https://download.documentfoundation.org/libreoffice/src/26.2.6/
  Linux distro build/source: https://packages.debian.org/bookworm/libreoffice
- CPython (PSF): https://www.python.org/downloads/release/python-31315/

Exact Python distribution versions are in requirements.lock.txt; installed
*.dist-info license files, Python LICENSE.txt, LibreOffice LICENSE.html,
license.txt and NOTICE are retained. No third-party source was modified.

Normal processing uses bundled local dependencies and frozen OCR models, not a
public conversion website. Python network calls are disabled. LibreOffice runs
with a private temporary profile, macro execution disabled, no link updating,
and external-resource prechecks. This is not an OS-level sandbox for arbitrary
malicious documents; the DSH Host's existing OS/container boundary still applies.

Never claim pixel-perfect Office compatibility: complex tracked changes, macros,
OLE, animations, Excel data connections and unsupported layout can be lost on
conversion. All outputs use new filenames and preserve original documents.
OCR extracts visible text, not general visual reasoning. Scan/table/handwriting
results must be reviewed. Formula evaluation uses LibreOffice compatibility,
not Microsoft's Excel calculation engine.
