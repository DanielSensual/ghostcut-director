from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

SRC_PATH = Path(__file__).resolve().parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

# Tests never run real Whisper. Code that finds `mlx_whisper` on PATH runs it, and inside a sandbox
# with no GPU MLX aborts and macOS shows a crash dialog per run. A stub that exits 1 shadows it for
# every test, so those paths see "Whisper failed" exactly as they would on a machine without it.
_STUB_DIR = Path(tempfile.mkdtemp(prefix="director-tests-bin-"))
_stub = _STUB_DIR / "mlx_whisper"
_stub.write_text("#!/bin/sh\necho 'mlx_whisper is stubbed out in tests' >&2\nexit 1\n")
_stub.chmod(0o755)
os.environ["PATH"] = f"{_STUB_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
atexit.register(shutil.rmtree, _STUB_DIR, ignore_errors=True)
