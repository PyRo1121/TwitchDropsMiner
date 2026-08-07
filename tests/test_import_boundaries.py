from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class ImportBoundaryTests(unittest.TestCase):
    def test_backend_import_does_not_load_qt(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        probe = "import sys; import twitch; assert 'gui_qt' not in sys.modules; assert 'PySide6' not in sys.modules"
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
