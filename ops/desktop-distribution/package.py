"""Build a public-only static distribution ZIP. Never reads signer secrets."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('distribution_verify', ROOT / 'verify-distribution.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def release_files(release):
    # Keep previous releases locally, but upload only the current signed release.
    if release['status'] == 'published':
        return ['site/releases/' + release['filename'], 'site/updates/company-win-x64.json']
    return []


def write_payload(archive, name, item):
    """Hash exactly the exported bytes; shell scripts always use Unix newlines."""
    info = ZipInfo.from_file(item, arcname='desktop-distribution/' + name)
    info.compress_type = ZIP_STORED if name.endswith('.exe') else ZIP_DEFLATED
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, 'w', force_zip64=True) as target:
        if name.endswith('.sh'):
            body = item.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
            target.write(body)
            digest.update(body)
            size = len(body)
        else:
            with item.open('rb') as source:
                for body in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(body)
                    digest.update(body)
                    size += len(body)
    return {'size_bytes': size, 'sha256': digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--allow-unpublished', action='store_true')
    args = parser.parse_args()
    release = verify.inspect_site(ROOT, require_published=not args.allow_unpublished)
    names = list((*verify.CODE, *verify.ASSETS))
    names += release_files(release)
    names = sorted(set(names))
    for name in names:
        verify.regular(ROOT, name)
    output = Path(args.output).absolute()
    if output.exists():
        raise SystemExit('OUTPUT_EXISTS: refusing to overwrite')
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {}
    with ZipFile(output, 'x', compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for name in names:
            item = verify.regular(ROOT, name)
            files[name] = write_payload(archive, name, item)
        archive.writestr('desktop-distribution/distribution-manifest.json',
                         json.dumps({'schema_version': 1, 'files': files}, indent=2) + '\n')
    print('DISTRIBUTION_PACKAGE:', output)
    print('DISTRIBUTION_SHA256:', verify.sha(output))
    print('RELEASE_STATUS:', release['status'])
    print('COMPANY_DEPLOYMENT: NOT_PERFORMED')


if __name__ == '__main__':
    main()
