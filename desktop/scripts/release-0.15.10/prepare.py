"""Materialize 0.15.10 from the exact internally published release and verified vendor archives."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
import zipfile
import zlib

TOOLS = Path(__file__).resolve().parent
SHELL = TOOLS.parents[1]
BASE = Path(os.environ['MAIMI_PREVIOUS_WORK']).resolve(strict=True)
WORK = Path(os.environ['MAIMI_WORK']).resolve()
if BASE == WORK or BASE.is_relative_to(WORK) or WORK.is_relative_to(BASE):
    raise ValueError('Use separate directories for the baseline and candidate')
HERE = WORK/'release-records'
HERE.mkdir(exist_ok=True,parents=True)
STAGE = WORK/'windows/win-unpacked'
OLD_STAGE = BASE/'windows/win-unpacked'
ARCHIVE = WORK/'validation/maimi-0.15.10.7z'
OLD_ARCHIVE = BASE/'validation/maimi-0.15.9.7z'
VENDORS = Path(os.environ['MAIMI_VENDOR_INPUTS']).resolve(strict=True)
CLI = Path(shutil.which('7z') or os.environ['MAIMI_7ZIP'])
VERSION = '0.15.10'
OLD = '0.15.9'
ELECTRON = '44.11.5'
UNDICI = {'resources/app/dsh/node_modules/undici':('8.11.2','8.13.1'),
          'resources/app/dsh/node_modules/@liustack/modlens/node_modules/undici':('8.12.3','8.13.1'),
          'resources/runtime/primary-runtime/dependencies/pnpm/dist/node_modules/undici':('6.29.0','6.29.1')}
VERSIONS = ('resources/app/package.json','resources/app/maimi/company-desktop-session.mjs')
DESCRIPTOR = 'resources/app/dsh/desktop-runtime.json'

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def read(path):
    return json.loads(path.read_bytes())

def save(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n','utf8')

def members(path):
    listing=subprocess.check_output([CLI,'l','-slt','-ba','-sccUTF-8',path]).decode('utf8').replace('\r\n','\n')
    result={}
    for block in listing.split('\n\n'):
        item=dict(line.split(' = ',1) for line in block.splitlines() if ' = ' in line)
        if item.get('Path') and 'Size' in item and not item.get('Attributes','').startswith('D'):
            size=int(item['Size'])
            if size and not item.get('CRC'):
                raise ValueError('Archive member lacks checksum '+item['Path'])
            result[item['Path'].replace('\\','/')]=(size,(item.get('CRC') or '00000000').upper())
    return result

def safe(name):
    path=PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Invalid archive path')
    return path.as_posix()

def vendor(name,version):
    rows=read(SHELL/'deps/windows-vendors-0.15.10.json')
    record,=[r for r in rows if (r['name'],r['version'])==(name,version)]
    path=VENDORS/record['filename']
    if digest(path)!=record['sha256']:
        raise ValueError('Vendor artifact changed '+name)
    return path

def package_path(name):
    return any(name.startswith(prefix+'/') for prefix in UNDICI)

def replace_component(name,current,path):
    prefix='resources/app/dsh/node_modules/'+name
    root=STAGE/prefix
    assert root.resolve()==root and root.resolve().is_relative_to(STAGE.resolve()) and root.is_dir()
    previous=read(root/'package.json')['version']
    if previous==current:raise ValueError('Expected a component update '+name)
    oldfiles=[p for p in root.rglob('*') if p.is_file() and p.relative_to(root).parts[0]!='node_modules']
    for old in oldfiles:
        assert old.resolve().is_relative_to(root)
        old.unlink()
    with tarfile.open(path) as archive:
        for member in archive:
            assert member.name.startswith('package/') and (member.isdir() or member.isfile())
            if member.isfile():
                rel=safe(member.name.removeprefix('package/'))
                if rel.startswith('node_modules/'):
                    raise ValueError('Unexpected bundled npm dependency')
                target=root/rel
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as out,archive.extractfile(member) as src:shutil.copyfileobj(src,out)
    assert read(root/'package.json')['version']==current
    return dict(root=prefix,previous=previous,current=current,sha256=digest(path))

def align_katex_cli():
    modules=STAGE/'resources/app/dsh/node_modules'
    local=modules/'katex/node_modules/commander'
    assert read(modules/'commander/package.json')['version']=='15.1.0'
    assert read(modules/'katex/package.json')['dependencies']['commander']=='^15.0.0'
    if local.is_dir():
        assert read(local/'package.json')['version']=='8.3.0'
        assert local.resolve()==local and local.resolve().is_relative_to(STAGE.resolve())
        assert all(p.resolve().is_relative_to(local) for p in local.rglob('*'))
        shutil.rmtree(local)

def extra():
    records=read(SHELL/'deps/windows-vendors-0.15.10.json')
    wanted={'@modelcontextprotocol/client','@modelcontextprotocol/core','fflate','katex'}
    chosen={r['name']:r for r in records if r['name'] in wanted}
    assert chosen.keys()==wanted
    assert chosen['@modelcontextprotocol/client']['version']==chosen['@modelcontextprotocol/core']['version']
    updated={}
    for name,record in chosen.items():
        pkg=VENDORS/record['filename']
        assert digest(pkg)==record['sha256']
        updated[name]=replace_component(name,record['version'],pkg)
    align_katex_cli()
    # Record exact company-side overrides of the immutable DSH package dependency pins.
    metadata=[]
    dsh=STAGE/'resources/app/dsh'
    package_files=[dsh/'package.json',*(dsh/'node_modules').glob('*/package.json'),*(dsh/'node_modules').glob('@*/*/package.json')]
    for file in package_files:
        data=read(file)
        before=(OLD_STAGE/file.relative_to(STAGE)).read_bytes()
        if data.get('name') in updated:continue
        deps=data.get('dependencies',{})
        changed=False
        for name,row in updated.items():
            if deps.get(name)==row['previous']:
                deps[name]=row['current']
                changed=True
        if changed:
            assert file.read_bytes()==before
            file.unlink()
            save(file,data)
            metadata.append(file.relative_to(STAGE).as_posix())
    subprocess.run(['node',TOOLS/'reseal.mjs',dsh],check=True)
    save(HERE/'extra-components.json',dict(components=updated,updated_exact_dependency_pins=metadata))

def prepare():
    assert digest(OLD_ARCHIVE)==read(SHELL/'deps/windows-release-0.15.10.json')['previousPayloadSha256']
    if STAGE.exists():
        raise FileExistsError('Use an empty version directory')
    expected=members(OLD_ARCHIVE)
    for name,(size,crc) in expected.items():
        file=OLD_STAGE/safe(name)
        if file.resolve()!=file or file.stat().st_size!=size:
            raise ValueError('Unverified previous application file '+name)
        with file.open('rb') as stream:
            actual=0
            for block in iter(lambda:stream.read(1024*1024),b''): actual=zlib.crc32(block,actual)
        if f'{actual:08X}' != crc: raise ValueError('Previous application CRC changed '+name)
    for folder in ('windows/win-unpacked','validation','upgrade-qa','source-assets','installer-ui','prepared/windows/electron'):
        (WORK/folder).mkdir(parents=True,exist_ok=True)
    for rel in ('installer-ui/window-frame.dll','source-assets/7z2606-extra.7z','source-assets/7z2606-src.7z'):
        os.link(BASE/rel,WORK/rel)
    # Materialize every unchanged company file as a hard link. Only candidate-owned files are written.
    directories=set()
    for name in expected:
        target=STAGE/safe(name)
        if not name.startswith('resources/') or package_path(name) or name in (*VERSIONS,DESCRIPTOR):
            continue
        if target.parent not in directories:
            target.parent.mkdir(parents=True,exist_ok=True)
            directories.add(target.parent)
        os.link(OLD_STAGE/name,target)
    for prefix,(previous,current) in UNDICI.items():
        assert read(OLD_STAGE/prefix/'package.json')['version']==previous
        with tarfile.open(vendor('undici',current)) as archive:
            for item in archive:
                assert item.name.startswith('package/') and (item.isfile() or item.isdir())
                if item.isfile():
                    target=STAGE/prefix/safe(item.name.removeprefix('package/'))
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with target.open('xb') as out, archive.extractfile(item) as src:
                        shutil.copyfileobj(src,out)
        assert read(STAGE/prefix/'package.json')['version']==current
    vendor_rows={}
    with zipfile.ZipFile(vendor('electron-windows-x64',ELECTRON)) as archive:
        for item in archive.infolist():
            if item.is_dir():continue
            name=safe(item.filename)
            if name=='resources/default_app.asar':
                # The company's application owns resources/app and does not ship the development default app.
                assert name not in expected
                continue
            target=WORK/'prepared/windows/electron/electron.exe' if name=='electron.exe' else STAGE/name
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as out,archive.open(item) as src:
                shutil.copyfileobj(src,out)
            vendor_rows[name]=digest(target)
    assert (STAGE/'version').read_text().strip()==ELECTRON
    for name in VERSIONS:
        old=(OLD_STAGE/name).read_bytes()
        assert old.count(OLD.encode())==1
        with (STAGE/name).open('xb') as f:f.write(old.replace(OLD.encode(),VERSION.encode()))
    env={**os.environ,'MAIMI_WORK':str(WORK)}
    subprocess.run(['node',SHELL/'scripts/brand.cjs'],env=env,check=True)
    # Reseal the new dependencies with the same project helper the DSH launcher uses.
    shutil.copyfile(OLD_STAGE/DESCRIPTOR,STAGE/DESCRIPTOR)
    subprocess.run(['node',TOOLS/'reseal.mjs',STAGE/'resources/app/dsh'],check=True)
    save(HERE/'vendor-files.json',{'upstreamElectron':ELECTRON,'shippedVendorFiles':vendor_rows,'undici':UNDICI,
                                   'excludedDevelopmentSampleApp':'resources/default_app.asar'})

def pack_payload():
    assert (HERE/'vendor-files.json').is_file()
    staging_receipt=HERE/'payload.json'
    if ARCHIVE.exists():
        assert not (WORK/'windows/release').exists() and digest(ARCHIVE)==read(staging_receipt)['sha256']
        base_archive=ARCHIVE
        dest=ARCHIVE.with_name('final-candidate.7z')
    else:
        base_archive=OLD_ARCHIVE
        dest=ARCHIVE
    previous=members(base_archive)
    extra_info=read(HERE/'extra-components.json') if (HERE/'extra-components.json').exists() else {}
    scopes=[*UNDICI,*(r['root'] for r in extra_info.get('components',{}).values())]
    embedded=read(HERE/'embedded-components.json')
    standalone=read(HERE/'standalone-node.json')
    office=read(HERE/'desktop-office.json')
    singles=(*VERSIONS,DESCRIPTOR,*extra_info.get('updated_exact_dependency_pins',[]),*embedded['paths'],*standalone['paths'],*office['paths'])
    explicit_removals=set(embedded['removed'])|set(office['removed'])
    assert not explicit_removals.intersection(singles)
    changed={n for n in previous if not n.startswith('resources/') or any(n.startswith(prefix+'/') for prefix in scopes)
             or n in singles or n in explicit_removals}
    new_names={p.relative_to(STAGE).as_posix() for p in STAGE.iterdir() if p.is_file()}
    for sub in ('locales',*scopes):
        new_names.update(p.relative_to(STAGE).as_posix() for p in (STAGE/sub).rglob('*') if p.is_file())
    new_names.update(singles)
    assert all(not (STAGE/n).exists() for n in explicit_removals)
    removed=changed-new_names
    obsolete_dirs=office['removed_directories']
    assert all(not (STAGE/safe(d)).exists() and not any(n.startswith(d+'/') for n in new_names) for d in obsolete_dirs)
    expected={n:r for n,r in previous.items() if n not in removed}
    updated=[]
    for name in sorted(new_names):
        crc=0
        with (STAGE/name).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):
                crc=zlib.crc32(block,crc)
        check=((STAGE/name).stat().st_size,f'{crc:08X}')
        if previous.get(name)!=check:updated.append(name)
        expected[name]=check
    if base_archive==OLD_ARCHIVE:
        assert DESCRIPTOR in updated
    elif not updated and not removed:
        assert all(n in previous for n in VERSIONS) and read(staging_receipt)['sha256']==digest(base_archive)
        return
    assert base_archive!=OLD_ARCHIVE or all(n in updated for n in ('Maimi.exe','version',*VERSIONS))
    assert not dest.exists()
    if removed:
        cut=WORK/'validation/omit.txt'
        cut.write_text('\n'.join(sorted(removed|set(obsolete_dirs)))+'\n','utf8')
        stage_archive=WORK/'validation/pruned.7z'
        shutil.copyfile(base_archive,stage_archive)
        subprocess.run([CLI,'d',stage_archive,'@'+str(cut),'-scsUTF-8','-bso0','-bsp0'],cwd=STAGE,check=True)
    else:
        stage_archive=base_archive
    changes=WORK/'validation/updated.txt'
    changes.write_text('\n'.join(updated)+'\n','utf8')
    subprocess.run([CLI,'u',stage_archive,'-u-','-up1q1r2x2y2z2w2!'+str(dest),'-mx=5','-ms=16m','-mmt=4',
                    '-scsUTF-8','@'+str(changes),'-bso0','-bsp0'],cwd=STAGE,check=True)
    assert members(dest)==expected
    subprocess.run([CLI,'t','-bso0','-bsp0',dest],check=True)
    if removed:stage_archive.unlink()
    if dest!=ARCHIVE:os.replace(dest,ARCHIVE)
    orig=members(OLD_ARCHIVE)
    save(HERE/'payload.json',dict(version=VERSION,sha256=digest(ARCHIVE),base_sha256=digest(OLD_ARCHIVE),
                                  bytes=ARCHIVE.stat().st_size,members=len(expected),changed=sum(orig.get(k)!=v for k,v in expected.items()),
                                  removed=len(orig.keys()-expected.keys())))

if __name__=='__main__':
    {'prepare':prepare,'extra':extra,'payload':pack_payload,'align-katex-cli':align_katex_cli}[sys.argv[1]]()
