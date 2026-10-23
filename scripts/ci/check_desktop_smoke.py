"""Smoke-test a frozen executable against isolated local data, without external services."""
import argparse
from contextlib import contextmanager
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def _command(executable, *args):
    return ([sys.executable, str(executable)] if executable.suffix == ".py" else [str(executable)]) + list(args)


def _local_client():
    # These probes target loopback only; runner/system proxies must not intercept them.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _log_files(root):
    return {"server.log": root / "server.log", "worker.log": root / "worker.log",
            "native.log": root / "native.log", "desktop.log": root / "user-data/logs/desktop.log"}


@contextmanager
def _smoke_workspace(workspace, diagnostics):
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=workspace) as temporary:
        root = Path(temporary)
        try:
            yield root
        except BaseException as exc:
            # Preserve only allowlisted logs, never the temporary credentials/database/profile.
            diagnostics.mkdir(parents=True, exist_ok=True)
            for name, path in _log_files(root).items():
                if path.is_file():
                    shutil.copyfile(path, diagnostics / name)
            (diagnostics / "failure.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            print(f"Desktop smoke diagnostics: {diagnostics}", file=sys.stderr)
            raise


def _wait_for_health(process, base, client, root, *, timeout):
    deadline = time.monotonic() + timeout
    last_error = "no health response"
    while True:
        code = process.poll()
        if code is not None or time.monotonic() >= deadline:
            logs = "\n".join(f"--- {name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-7000:]}"
                             for name, path in _log_files(root).items() if path.is_file())
            raise RuntimeError(f"Desktop server did not become healthy at {base}: "
                               f"exit_code={code}, last_error={last_error}\n{logs}")
        try:
            with client.open(base + "/health", timeout=1) as response:
                assert json.load(response)["status"] == "healthy"
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
            time.sleep(0.2)


def _stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=20)


def check(executable: Path, workspace: Path, *, native=False, report=None, startup_timeout=120) -> None:
    if startup_timeout <= 0:
        raise ValueError("startup_timeout must be positive")
    workspace.mkdir(parents=True, exist_ok=True)
    report = (report or workspace / "desktop-gui.json").resolve()
    diagnostics = report.with_name(report.stem + "-logs")
    client = _local_client()
    with _smoke_workspace(workspace, diagnostics) as root:
        assert root.resolve().is_relative_to(workspace.resolve())
        data = root / "user-data"
        env = os.environ.copy()
        env.update(GOOFISH_DATA_DIR=str(data), APP_DATABASE_FILE=str(data / "data/app.sqlite3"),
                   TASK_FAILURE_GUARD_PATH=str(data / "logs/guard.json"),
                   WEB_USERNAME="desktop-smoke", WEB_PASSWORD="smoke-only", WEB_COOKIE_SECURE="false")
        worker_log = root / "worker.log"
        for attempt in range(2):
            result = subprocess.run(_command(executable, "--spider-worker", "--worker-log", str(worker_log), "--help"),
                                    env=env, cwd=root, capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError(f"Worker failed ({result.returncode}): {result.stderr.decode('utf-8', errors='replace')}")
            assert "--task-id" in worker_log.read_text(encoding="utf-8")
            assert (data / "prompts/base_prompt.txt").is_file()
            settings = data / ".env"
            if attempt == 0:
                with settings.open("a", encoding="utf-8") as output:
                    output.write("\nSMOKE_PERSISTENCE=yes\n")
            assert "SMOKE_PERSISTENCE=yes" in settings.read_text(encoding="utf-8")

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            env["SERVER_PORT"] = str(listener.getsockname()[1])
        base = "http://127.0.0.1:" + env["SERVER_PORT"]
        with (root / "server.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen(_command(executable, "--no-browser"), env=env, cwd=root,
                                       stdout=output, stderr=output, start_new_session=os.name != "nt")
            try:
                _wait_for_health(process, base, client, root, timeout=startup_timeout)
                with client.open(base, timeout=5) as response:
                    html = response.read().decode("utf-8")
                asset = re.search(r'src="(/assets/[^\"]+)"', html)
                assert asset, "Packaged frontend entry is missing"
                with client.open(base + asset.group(1), timeout=5) as response:
                    assert response.status == 200
                try:
                    client.open(base + "/api/tasks", timeout=5)
                except urllib.error.HTTPError as error:
                    assert error.code == 401
                else:
                    raise AssertionError("Unauthenticated API request was accepted")
                request = urllib.request.Request(base + "/auth/status", data=json.dumps(
                    {"username": env["WEB_USERNAME"], "password": env["WEB_PASSWORD"]}).encode(),
                    headers={"Content-Type": "application/json"})
                with client.open(request, timeout=5) as response:
                    assert response.status == 200
                with client.open(base + "/api/tasks", timeout=5) as response:
                    assert response.status == 200
                # Verify the frozen app contains the new QR routes, without
                # contacting Goofish or creating any real login sessions.
                invalid_login = urllib.request.Request(base + "/api/accounts/qr-login",
                    data=json.dumps({"name": "../invalid"}).encode(),
                    headers={"Content-Type": "application/json"})
                try:
                    client.open(invalid_login, timeout=5)
                except urllib.error.HTTPError as error:
                    assert error.code == 400
                else:
                    raise AssertionError("Packaged QR login route did not validate account names")
                try:
                    client.open(base + "/api/accounts/qr-login/smoke-missing", timeout=5)
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                    assert "扫码会话" in json.load(error)["detail"]
                else:
                    raise AssertionError("Packaged QR session route is missing")
                assert (data / "data/app.sqlite3").is_file()
            finally:
                _stop_process(process)
        if native:
            report.unlink(missing_ok=True)
            with (root / "native.log").open("w", encoding="utf-8") as output:
                process = subprocess.Popen(_command(executable, "--smoke-report", str(report)),
                    env=env, cwd=root, stdout=output, stderr=output, start_new_session=os.name != "nt")
                try:
                    process.wait(timeout=120)
                finally:
                    _stop_process(process)
            if process.returncode or not report.is_file():
                log = data / "logs/desktop.log"
                diagnostic = log.read_text(encoding="utf-8", errors="replace")[-7000:] if log.exists() else (root / "native.log").read_text(encoding="utf-8", errors="replace")[-7000:]
                raise RuntimeError("Native desktop failed: " + diagnostic)
            payload = json.loads(report.read_text(encoding="utf-8"))
            assert payload["native_window"] and payload["tray_hide_restore"] and payload["icon_loaded"]
            assert payload["account_qr_dialog"], "Packaged account dialog does not expose QR login"
            # Exiting through the tray must also stop the embedded server.
            try:
                client.open(payload["url"] + "/health", timeout=1)
            except (OSError, urllib.error.URLError):
                pass
            else:
                raise AssertionError("Native desktop left its HTTP server running")
    print("Desktop smoke passed: worker dispatch, persistent data, packaged SPA/assets, health, API authentication and QR routes"
          + (", native window, tray and QR account dialog" if native else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("audit/desktop/smoke"))
    parser.add_argument("--native", action="store_true", help="Also launch the actual Windows window and exercise its tray")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=120, help="Maximum server startup wait in seconds")
    args = parser.parse_args()
    check(args.executable.resolve(), args.workspace.resolve(), native=args.native, report=args.report,
          startup_timeout=args.startup_timeout)
