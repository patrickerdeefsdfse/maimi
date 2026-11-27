"""Build-time packaging only; no CLI execution, auth, network, or deletion."""
import os, argparse, hashlib, json, pathlib, stat, zipfile

HERE = pathlib.Path(__file__).resolve().parent
DEPENDENCIES = HERE.parent / 'deps' / 'dws.json'
PINS = {
    'dws-windows-amd64.zip': ('64dcf850b32db1e07861ba971a9a251d1ed95a86bbe5bb248866377a67520d53', 21025934, 'bin'),
    'dws-skills.zip': ('30a872a3b472af235baf633662d26ad1ddd0c47d75c31b4452b2ea82dd966e44', 3317903, 'skills'),
}

def safe_directory(directory):
    for current in [*reversed(directory.parents), directory]:
        if current.exists():
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('TARGET_DIRECTORY_LINK_OR_NOT_DIRECTORY')
        else:
            current.mkdir()

def stage(destination):
    if not destination.is_absolute():
        raise ValueError('ABSOLUTE_DESTINATION_REQUIRED')
    safe_directory(destination.parent)
    if destination.exists():
        raise ValueError('NEW_DESTINATION_REQUIRED')
    dependencies = json.loads(DEPENDENCIES.read_text(encoding='utf-8'))
    artifacts = dependencies['artifacts']
    if {a['name'] for a in artifacts} != set(PINS):
        raise ValueError('ARTIFACT_SET_CHANGED')
    payloads = []
    for artifact in artifacts:
        digest, size, prefix = PINS[artifact['name']]
        archive = pathlib.Path(os.environ['MAIMI_WORK']).resolve() / 'downloads' / artifact['name']
        data = archive.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('ARTIFACT_PIN_MISMATCH')
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                relative = pathlib.PurePosixPath(entry.filename)
                if relative.is_absolute() or '\\' in entry.filename or any(p in ('', '.', '..') for p in entry.filename.split('/')):
                    raise ValueError('ARCHIVE_PATH_REFUSED')
                if entry.is_dir():
                    continue
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.file_size > 100*1024*1024:
                    raise ValueError('ARCHIVE_ENTRY_REFUSED')
                content = source.read(entry)
                payloads.append((pathlib.PurePosixPath(prefix) / relative, content))
    if len({p for p, _ in payloads}) != len(payloads):
        raise ValueError('DUPLICATE_RESOURCE')
    destination.mkdir()
    manifest = {'schema': 'maimi-desktop-dws/v1', 'version': '1.0.66', 'files': []}
    for relative, content in payloads:
        output = destination.joinpath(*relative.parts)
        safe_directory(output.parent)
        with output.open('xb') as stream:
            stream.write(content)
        manifest['files'].append({'path': relative.as_posix(), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return {'status': 'STAGED', 'version': manifest['version'], 'files': len(payloads),
            'bytes': sum(len(content) for _, content in payloads), 'destination': str(destination),
            'programs_executed': False, 'accounts_connected': False}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(stage(args.destination), ensure_ascii=False))
