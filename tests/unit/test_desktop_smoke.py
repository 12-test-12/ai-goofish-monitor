import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

from scripts.ci import check_desktop_smoke as smoke


def test_loopback_health_probe_bypasses_runner_proxies(tmp_path, monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy"}).encode())

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            smoke._wait_for_health(SimpleNamespace(poll=lambda: None),
                f"http://127.0.0.1:{server.server_port}", smoke._local_client(), tmp_path, timeout=2)
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_exited_process_reports_exit_code_and_startup_traceback(tmp_path):
    logs = tmp_path / "user-data/logs"
    logs.mkdir(parents=True)
    (logs / "desktop.log").write_text("Traceback: missing packaged dependency", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exit_code=23") as caught:
        smoke._wait_for_health(SimpleNamespace(poll=lambda: 23),
            "http://127.0.0.1:1", smoke._local_client(), tmp_path, timeout=2)
    assert "missing packaged dependency" in str(caught.value)


def test_health_deadline_reports_live_but_unready_process(tmp_path):
    with pytest.raises(RuntimeError, match="exit_code=None"):
        smoke._wait_for_health(SimpleNamespace(poll=lambda: None),
            "http://127.0.0.1:1", smoke._local_client(), tmp_path, timeout=0)


def test_failed_executable_keeps_only_diagnostic_logs(tmp_path):
    # Exercise the public command's subprocess/temporary-directory path.
    executable = tmp_path / "failed_app.py"
    executable.write_text('''
import os
from pathlib import Path
import sys
data = Path(os.environ["GOOFISH_DATA_DIR"])
(data / "prompts").mkdir(parents=True, exist_ok=True)
(data / "prompts/base_prompt.txt").write_text("test", encoding="utf-8")
settings = data / ".env"
if not settings.exists():
    settings.write_text("PRIVATE_TEST_VALUE=must-not-be-copied", encoding="utf-8")
if "--spider-worker" in sys.argv:
    Path(sys.argv[sys.argv.index("--worker-log") + 1]).write_text("--task-id", encoding="utf-8")
else:
    (data / "logs").mkdir(exist_ok=True)
    (data / "logs/desktop.log").write_text("synthetic startup failure", encoding="utf-8")
    raise SystemExit(23)
''', encoding="utf-8")
    workspace = tmp_path / "smoke"
    report = tmp_path / "reports/desktop-gui.json"
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        smoke.check(executable, workspace, report=report, startup_timeout=5)
    diagnostics = report.with_name("desktop-gui-logs")
    assert (diagnostics / "desktop.log").read_text(encoding="utf-8") == "synthetic startup failure"
    assert "exit_code=23" in (diagnostics / "failure.txt").read_text(encoding="utf-8")
    assert {file.name for file in diagnostics.iterdir()} == {"server.log", "worker.log", "desktop.log", "failure.txt"}
    assert not list(workspace.iterdir())
    assert not report.exists()
