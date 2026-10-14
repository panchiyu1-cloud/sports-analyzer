import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import conftest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class FootballCsvCheckTests(unittest.TestCase):
    """scripts/is_football_csv.sh decides whether a download replaces the file we have."""

    def check(self, content: bytes) -> bool:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as fh:
            fh.write(content)
        try:
            run = subprocess.run(["bash", str(ROOT / "scripts" / "is_football_csv.sh"), fh.name])
            return run.returncode == 0
        finally:
            Path(fh.name).unlink()

    def test_files_with_a_byte_order_mark_are_csv(self):
        # Regression: since 2024/25 the files start with a BOM and were all rejected.
        self.assertTrue(self.check(b"\xef\xbb\xbfDiv,Date,Time,HomeTeam\nE0,16/08/2024"))
        self.assertTrue(self.check(b"Div,Date,HomeTeam\nE0,16/08/2019"))

    def test_error_pages_are_not(self):
        self.assertFalse(self.check(b"<!DOCTYPE html><html><body>Not found</body></html>"))
        self.assertFalse(self.check(b""))


if __name__ == "__main__":
    unittest.main()
