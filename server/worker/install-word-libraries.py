"""Install pinned authoring packages from the company's immutable Office image."""
import argparse
import importlib.metadata as metadata
from pathlib import Path
import shutil
import stat
import sysconfig


GROUPS = {
    'word': {'python-docx': '1.2.0', 'lxml': '6.1.3', 'typing-extensions': '4.16.0'},
    'spreadsheet-image': {'openpyxl': '3.1.5', 'et-xmlfile': '2.0.0',
                          'defusedxml': '0.7.1', 'Pillow': '12.3.0'},
}


def install(source, destination, packages):
    names = {name.lower().replace('-', '_'): expected for name, expected in packages.items()}
    distributions = {dist.metadata['Name'].lower().replace('-', '_'): dist
                     for dist in metadata.distributions(path=[str(source)])}
    if sysconfig.get_config_var('SOABI') != 'cpython-313-x86_64-linux-gnu':
        raise RuntimeError('Office dependencies require the reviewed CPython 3.13 Linux x86_64 runtime')
    for name, version in names.items():
        dist = distributions[name]
        if dist.version != version or not dist.files:
            raise RuntimeError(f'Unexpected Office dependency: {name}')
        for item in dist.files:
            if '__pycache__' in item.parts or item.suffix == '.pyc':
                continue
            if item.is_absolute() or '..' in item.parts:
                raise ValueError(f'Unexpected package member: {item}')
            src = Path(dist.locate_file(item))
            if not src.resolve(strict=True).is_relative_to(source) or not src.is_file():
                raise ValueError(f'Invalid package source: {item}')
            target = destination.joinpath(*item.parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            target.chmod(stat.S_IMODE(src.stat().st_mode) & 0o755)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', choices=tuple(GROUPS), action='append')
    packages = {}
    for group in parser.parse_args().group or ['word']:
        packages.update(GROUPS[group])
    install(Path('/opt/approved-word-python').resolve(strict=True), Path(sysconfig.get_path('purelib')), packages)
