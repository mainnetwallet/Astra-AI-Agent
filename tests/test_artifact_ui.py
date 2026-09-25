"""Run the generated-artifact frontend regression under Node."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestArtifactUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_backend_shaped_image_artifact_regression(self):
        proc = subprocess.run(
            ["node", "--test", "tests/js/artifact_ui.test.js"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"node test failed:\n{proc.stdout}\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()