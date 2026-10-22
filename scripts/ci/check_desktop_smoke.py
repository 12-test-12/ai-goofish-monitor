"""Smoke-test a frozen executable against isolated local data, without external services."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def check(executable: Path, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=workspace) as temporary:
        root = Path(temporary)
        assert root.resolve().is_relative_to(workspace.resolve())
        data = root / "user-data"
        env = os.environ.copy()
        env.update(GOOFISH_DATA_DIR=str(data), APP_DATABASE_FILE=str(data / "data/app.sqlite3"),
                   TASK_FAILURE_GUARD_PATH=str(data / "logs/guard.json"),
                   WEB_USERNAME="desktop-smoke", WEB_PASSWORD="smoke-only", WEB_COOKIE_SECURE="false")
        worker_log = root / "worker.log"
        for attempt in range(2):
            result = subprocess.run([str(executable), "--spider-worker", "--worker-log", str(worker_log), "--help"],
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
            process = subprocess.Popen([str(executable), "--no-browser"], env=env, cwd=root,
                                       stdout=output, stderr=output, start_new_session=os.name != "nt")
            try:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        with urllib.request.urlopen(base + "/health", timeout=1) as response:
                            assert json.load(response)["status"] == "healthy"
                        break
                    except (OSError, urllib.error.URLError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Desktop server did not become healthy")
                        time.sleep(0.2)
                with urllib.request.urlopen(base, timeout=5) as response:
                    html = response.read().decode("utf-8")
                asset = re.search(r'src="(/assets/[^\"]+)"', html)
                assert asset, "Packaged frontend entry is missing"
                with urllib.request.urlopen(base + asset.group(1), timeout=5) as response:
                    assert response.status == 200
                try:
                    urllib.request.urlopen(base + "/api/tasks", timeout=5)
                except urllib.error.HTTPError as error:
                    assert error.code == 401
                else:
                    raise AssertionError("Unauthenticated API request was accepted")
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                request = urllib.request.Request(base + "/auth/status", data=json.dumps(
                    {"username": env["WEB_USERNAME"], "password": env["WEB_PASSWORD"]}).encode(),
                    headers={"Content-Type": "application/json"})
                with client.open(request, timeout=5) as response:
                    assert response.status == 200
                with client.open(base + "/api/tasks", timeout=5) as response:
                    assert response.status == 200
                assert (data / "data/app.sqlite3").is_file()
            finally:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                   capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=20)
    print("Desktop smoke passed: worker dispatch, persistent data, packaged SPA/assets, health and API authentication")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("audit/desktop/smoke"))
    args = parser.parse_args()
    check(args.executable.resolve(), args.workspace.resolve())
