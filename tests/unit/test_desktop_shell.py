import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import desktop_launcher as launcher
from src.desktop.shell import DesktopTray, wait_for_server


def test_window_close_hides_only_when_the_tray_can_restore_it():
    window = Mock()
    tray = DesktopTray(window, Path("icon.png"))
    assert tray.on_closing() is True
    window.hide.assert_not_called()
    tray.ready.set()
    assert tray.on_closing() is False
    window.hide.assert_called_once()
    tray.show()
    window.show.assert_called_once()
    window.restore.assert_called_once()
    tray.quit()
    window.destroy.assert_called_once()
    assert tray.on_closing() is True


def test_server_start_failure_and_timeout_do_not_open_a_window():
    server = SimpleNamespace(started=False)
    with pytest.raises(RuntimeError):
        wait_for_server(server, SimpleNamespace(is_alive=lambda: False), [])
    with pytest.raises(RuntimeError):
        wait_for_server(server, SimpleNamespace(is_alive=lambda: True), [], timeout=0)
    with pytest.raises(RuntimeError) as caught:
        wait_for_server(server, threading.current_thread(), [ValueError("startup")])
    assert isinstance(caught.value.__cause__, ValueError)


def test_default_entry_opens_native_shell_and_headless_entry_stays_headless(monkeypatch):
    native, headless = Mock(), Mock()
    monkeypatch.setattr(launcher, "_prepare_environment", lambda: None)
    monkeypatch.setattr(launcher, "_configure_desktop_logging", lambda: None)
    monkeypatch.setattr(launcher, "run_app", headless)
    monkeypatch.setattr("src.desktop.shell.run_desktop", native)
    launcher.main([])
    native.assert_called_once_with(launcher.BASE_DIR, smoke_report=None)
    headless.assert_not_called()
    launcher.main(["--no-browser"])
    headless.assert_called_once_with(open_browser=False)


def test_server_readiness_accepts_only_its_own_started_server():
    wait_for_server(SimpleNamespace(started=True), SimpleNamespace(is_alive=lambda: True), [])
