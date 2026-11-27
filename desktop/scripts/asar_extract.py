"""Materialize the cryptographically pinned upstream Desktop package for company staging."""
import hashlib
import json
import re
import shutil
import struct
from pathlib import Path

def extract(official, target, version):
    target.mkdir(parents=True, exist_ok=False)
    unpacked = official.with_name('app.asar.unpacked')
    with official.open('rb') as archive:
        size, header_size, payload_size, json_size = struct.unpack('<IIII',archive.read(16))
        assert size == 4 and 0 < json_size < payload_size <= header_size < official.stat().st_size
        header = json.loads(archive.read(json_size))
        content_start = 8 + header_size
        count = 0
        def extract(members, prefix):
            nonlocal count
            for name,node in members.items():
                if name in ('.','..') or re.search(r'[\\/:]',name):
                    raise ValueError('unsafe ASAR member')
                rel=prefix/name
                dest=target/rel
                if 'files' in node:
                    dest.mkdir()
                    extract(node['files'],rel)
                    continue
                if 'link' in node:
                    raise ValueError('linked ASAR member: '+str(rel))
                if node.get('unpacked'):
                    src=unpacked/rel
                    assert src.is_file() and not src.is_symlink() and src.stat().st_size == node['size']
                    shutil.copyfile(src, dest)
                else:
                    start=content_start+int(node['offset'])
                    assert start>=content_start and start+node['size']<=official.stat().st_size
                    archive.seek(start)
                    data=archive.read(node['size'])
                    assert len(data)==node['size']
                    if integrity:=node.get('integrity'):
                        assert integrity['algorithm']=='SHA256' and hashlib.sha256(data).hexdigest()==integrity['hash']
                    dest.write_bytes(data)
                count+=1
        extract(header['files'],Path())
    for name in ('dsh','dsh-app-boot','dsh-office-kit','dsh-web-app','dsh-desktop-host'):
        assert json.loads((target/'dsh/node_modules/@deepseek-ai'/name/'package.json').read_text(encoding='utf-8'))['version']==version
    print('Verified',count,'ASAR members and pinned release packages')
