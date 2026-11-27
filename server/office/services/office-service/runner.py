"""Fixed child bootstrap: resource bounds apply before importing the engine."""
import os
from pathlib import Path
import resource
import runpy
import sys

script = Path(sys.argv[1])
if not script.is_absolute() or not script.is_file() or script.is_symlink():
    raise SystemExit(2)
memory = int(os.environ['OFFICE_CHILD_MEMORY_BYTES'])
seconds = int(os.environ['OFFICE_CHILD_CPU_SECONDS'])
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 2))
resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024**2, 256 * 1024**2))
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.argv = [str(script)]
runpy.run_path(str(script), run_name='__main__')
