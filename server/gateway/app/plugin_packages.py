"""Bounded, non-executable interpretation of administrator-supplied DSH bundles."""
import base64
import binascii
import hashlib
import io
import json
import re
import stat
import unicodedata
import zlib
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from fastapi import HTTPException

MAX_ARCHIVE = 4 * 1024 * 1024
KEY = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')
VERSION = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}')
FILES = {'manifest.json', 'SKILL.md', 'package.json', 'index.mjs'}
def fail(code):
    raise HTTPException(422, code)


def _json(data):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('plugin_duplicate_json_field')
            result[key] = value
        return result
    result = json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique)
    if not isinstance(result, dict):
        fail('plugin_invalid_manifest')
    return result


def parse_package(filename, encoded):
    if not filename.lower().endswith('.zip') or len(encoded) > 4*((MAX_ARCHIVE+2)//3):
        fail('plugin_zip_required_or_too_large')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw)>MAX_ARCHIVE or not raw.startswith(b'PK\x03\x04'):
            fail('plugin_zip_invalid')
        with ZipFile(io.BytesIO(raw)) as bundle:
            entries = bundle.infolist()
            if len(entries) not in (2,3) or sum(z.file_size for z in entries)>8*1024*1024:
                fail('plugin_archive_limits')
            files = {}
            for z in entries:
                if (z.orig_filename not in FILES or z.filename!=z.orig_filename or z.filename in files
                    or z.flag_bits & 1 or stat.S_IFMT(z.external_attr>>16) not in (0,stat.S_IFREG)
                    or z.compress_type not in (ZIP_STORED,ZIP_DEFLATED)):
                    fail('plugin_archive_invalid_entry')
                limit = {'manifest.json':8192,'package.json':16384,'SKILL.md':131072,'index.mjs':8*1024*1024}[z.filename]
                if not 0<z.file_size<=limit:
                    fail('plugin_entry_size')
                with bundle.open(z) as reader:
                    content=reader.read(limit+1)
                if len(content)!=z.file_size:
                    fail('plugin_invalid_size')
                files[z.filename]=content
        manifest=_json(files['manifest.json'])
        if set(manifest)!={'plugin_key','version','name','description','kind'}:
            fail('plugin_manifest_fields')
        if not all(isinstance(v,str) for v in manifest.values()):
            fail('plugin_manifest_strings')
        key,ver=manifest['plugin_key'],manifest['version']
        if len(key)>64 or not KEY.fullmatch(key) or not VERSION.fullmatch(ver):
            fail('plugin_invalid_key_or_version')
        for field,maximum in [('name',100),('description',500)]:
            value=manifest[field]
            if (not 0<len(value.strip())<=maximum or value!=value.strip()
                or any(unicodedata.category(ch) in ('Cc','Cf','Cs') for ch in value)):
                fail('plugin_invalid_display_text')
        instructions=None
        if manifest['kind']=='skill' and set(files)=={'manifest.json','SKILL.md'}:
            instructions=files['SKILL.md'].decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n').strip()
            if not instructions or '\x00' in instructions:
                fail('plugin_empty_skill')
        elif manifest['kind']=='native' and set(files)=={'manifest.json','package.json','index.mjs'}:
            package=_json(files['package.json'])
            if (package.get('type')!='module' or package.get('version')!=ver or package.get('main') not in ('index.mjs','./index.mjs')
                or not isinstance(package.get('name'),str) or not package['name']
                or any(k in package for k in ('scripts','dependencies','devDependencies','optionalDependencies','peerDependencies','bin','exports','imports'))):
                fail('plugin_native_layout')
            if not files['index.mjs'].decode('utf8').strip() or b'\0' in files['index.mjs']:
                fail('plugin_invalid_native_source')
        else:
            fail('plugin_kind_or_layout')
    except (BadZipFile, UnicodeError, ValueError, KeyError, OSError, EOFError, RuntimeError, RecursionError, zlib.error, binascii.Error) as exc:
        raise HTTPException(422,'plugin_archive_unreadable') from exc
    return manifest, raw, hashlib.sha256(raw).hexdigest(), instructions
