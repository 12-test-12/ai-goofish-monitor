import subprocess
import sys
from pathlib import Path


def test_import_keeps_captured_or_missing_stdio_untouched():
    script = "import io, sys; stream=io.StringIO(); sys.stdout=stream; sys.stderr=None; import src.ai_handler; assert sys.stdout is stream; assert sys.stderr is None; sys.__stdout__.write('OK')"
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK"
