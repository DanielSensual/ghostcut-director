"""Shadow `mlx_whisper` for the whole test run.

`discover -s tests` imports test modules by file name (this one sorts first) and never imports
tests/__init__.py, so the guard lives in a test module: every module is imported before any test runs.
Code that finds `mlx_whisper` on PATH runs it; inside a sandbox with no GPU, MLX aborts and macOS shows
a crash dialog per run. Tests must never run Whisper anyway.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_STUB_DIR = Path(tempfile.mkdtemp(prefix="director-tests-bin-"))
_stub = _STUB_DIR / "mlx_whisper"
_stub.write_text("#!/bin/sh\necho 'mlx_whisper is stubbed out in tests' >&2\nexit 1\n")
_stub.chmod(0o755)
os.environ["PATH"] = f"{_STUB_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
atexit.register(shutil.rmtree, _STUB_DIR, ignore_errors=True)


class NoWhisperGuardTests(unittest.TestCase):
    def test_mlx_whisper_resolves_to_the_stub(self) -> None:
        self.assertEqual(shutil.which("mlx_whisper"), str(_stub))
