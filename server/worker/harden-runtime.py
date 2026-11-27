"""Verify immutable program ownership and remove non-owner write bits while building the image."""
import os
from pathlib import Path
import stat


def harden(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError(f"Unexpected program entry type at {path}")
    if info.st_uid != 0:
        raise ValueError(f"Expected root-owned program entry at {path}")
    if info.st_mode & 0o022:
        path.chmod(stat.S_IMODE(info.st_mode) & ~0o022)


for root in (Path("/opt/dsh"), Path("/opt/personal-cloud")):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Missing program directory {root}")
    harden(root)
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            harden(Path(directory) / name)
