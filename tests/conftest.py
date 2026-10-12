"""Make `src/` importable for both pytest and `python -m unittest`."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Fixture files deliberately contain one broken record; keep that warning out of the test output.
import logging  # noqa: E402

logging.getLogger("sports_analyzer").setLevel(logging.ERROR)
