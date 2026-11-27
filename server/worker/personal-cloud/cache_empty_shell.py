"""Remove only empty, old DSH staging directories, while the manager holds both worker fences."""
import os
import re
import stat
import time

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
SHAPE = re.compile(r'attachments(?:/v1(?:/tmp|/file-objects(?:/[a-f0-9]{2})?|/files(?:/[a-f0-9]{2}(?:/[a-f0-9]{64})?)?)?)?')

def clean_empty_shell(parent, name, root, deadline):
    if not re.fullmatch(r'cache-[a-zA-Z0-9]{6}', name):
        return None
    info = os.fstat(root)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or time.time() - info.st_mtime < 3600:
        return None
    opened = []
    def inventory(fd, prefix):
        names = os.listdir(fd)
        if len(names) > 12:
            return False
        for leaf in names:
            rel = prefix + '/' + leaf if prefix else leaf
            if len(opened) >= 24 or time.monotonic() > deadline or not SHAPE.fullmatch(rel):
                return False
            child = os.open(leaf, FLAGS, dir_fd=fd)
            opened.append((fd, leaf, child))
            st = os.fstat(child)
            if st.st_dev != info.st_dev or st.st_uid != info.st_uid or st.st_mode & 0o022 or not inventory(child, rel):
                return False
        return True
    try:
        if not inventory(root, ''):
            return None
        # All entries were opened as directories. Never unlink a file or follow a symlink.
        for fd, leaf, child in [*reversed(opened), (parent, name, root)]:
            if time.monotonic() > deadline or not os.path.samestat(os.fstat(child), os.stat(leaf, dir_fd=fd, follow_symlinks=False)):
                return None
            os.rmdir(leaf, dir_fd=fd)
        return 0
    finally:
        for _, _, child in reversed(opened):
            os.close(child)
