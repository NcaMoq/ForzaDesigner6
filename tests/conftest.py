import sys
from pathlib import Path

# Make the repo importable when running `pytest` from FD6/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
