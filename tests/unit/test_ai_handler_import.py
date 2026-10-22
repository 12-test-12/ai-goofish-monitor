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
        import io, sys, threading
        real_err = sys.__stderr__
        stream = None if {missing_stdout!r} else io.StringIO()
        sys.stdout, sys.stderr = stream, None
        try:
            import desktop_launcher as launcher
            calls = {{}}
            opened = threading.Event()
            def open_browser(url):
                calls.update(url=url)
                opened.set()
            launcher.webbrowser.open = open_browser
            launcher.uvicorn.Config = lambda app, **kwargs: calls.update(app=app, **kwargs)
            class Server:
                started = False
                should_exit = False
                def __init__(self, _config):
                    pass
                def run(self):
                    self.started = True
                    assert opened.wait(timeout=2)
            launcher.uvicorn.Server = Server
            launcher.run_app()
            assert calls['host'] == '127.0.0.1'
            assert calls['app'] is not None
            assert calls['url'].endswith(str(calls['port']))
            assert sys.stdout is stream and sys.stderr is None
            sys.__stdout__.write('OK')
        except ModuleNotFoundError as err:
            real_err.write(f'MODULE_NOT_FOUND: {{err}}\\n')
            sys.exit(77)
        except Exception as exc:
            real_err.write(f'ERROR: {{exc}}\\n')
            sys.exit(1)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 77:
        pytest.skip(result.stderr.strip())
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK"
