"""OS-owned single-controller lock; automatically released if the process exits."""
import os
from pathlib import Path
import stat


class ControllerLock:
    def __init__(self, database):
        path = Path(database).absolute().with_name(Path(database).name + '.controller-lock')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        self.fd = os.open(path, flags, 0o600)
        try:
            opened, named = os.fstat(self.fd), path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(named.st_mode)
                    or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                raise OSError('invalid controller lock file')
            if os.name == 'nt':
                import msvcrt
                if opened.st_size == 0:
                    os.write(self.fd, b'\x00')
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

