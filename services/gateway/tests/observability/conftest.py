from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
GATEWAY = HERE.parents[1]
sys.path.insert(0,str(GATEWAY))
sys.path.insert(0,str(HERE))
