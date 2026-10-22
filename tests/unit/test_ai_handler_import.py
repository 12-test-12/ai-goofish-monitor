import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def test_import_keeps_captured_or_missing_stdio_untouched():
    script = "import io, sys; stream=io.StringIO(); sys.stdout=stream; sys.stderr=None; import src.ai_handler; assert sys.stdout is stream; assert sys.stderr is None; sys.__stdout__.write('OK')"
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK"


@pytest.mark.parametrize("missing_stdout", [False, True])
def test_desktop_source_entry_supports_captured_or_missing_stdio(missing_stdout):
    script = textwrap.dedent(f"""
        import io, sys
        stream = None if {missing_stdout!r} else io.StringIO()
        sys.stdout, sys.stderr = stream, None
        import desktop_launcher as launcher
        calls = {{}}
        launcher.webbrowser.open = lambda url: calls.update(url=url)
        launcher.time.sleep = lambda _: None
        launcher.uvicorn.run = lambda app, **kwargs: calls.update(app=app, **kwargs)
        launcher.run_app()
        assert calls['host'] == '127.0.0.1'
        assert calls['app'] is not None
        assert calls['url'].endswith(str(calls['port']))
        assert sys.stdout is stream and sys.stderr is None
        sys.__stdout__.write('OK')
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK"
