import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

from src.services import browser_launcher as launcher


def missing(channel):
    return PlaywrightError(f"Chromium distribution '{channel}' is not found at C:/missing/browser.exe")


def run_launch(sync, launch, options, **kwargs):
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    if sync:
        return launcher.launch_browser_sync(playwright, options, **kwargs)
    return asyncio.run(launcher.launch_browser(playwright, options, **kwargs))


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("preferred,fallback", [("chrome", "msedge"), ("msedge", "chrome")])
def test_missing_preferred_browser_uses_other_installed_browser(sync, preferred, fallback, capsys):
    browser = object()
    launch = (Mock if sync else AsyncMock)(side_effect=[missing(preferred), browser])
    options = {"channel": preferred, "headless": False, "args": ["--test-arg"],
               "proxy": {"server": "http://proxy.example:8080"}, "timeout": 15000}
    assert run_launch(sync, launch, options) is browser
    assert [call.kwargs["channel"] for call in launch.call_args_list] == [preferred, fallback]
    for call in launch.call_args_list:
        assert {key: value for key, value in call.kwargs.items() if key != "channel"} == {
            key: value for key, value in options.items() if key != "channel"
        }
    assert options["channel"] == preferred
    assert "尝试" in capsys.readouterr().out


@pytest.mark.parametrize("sync", [False, True])
def test_falls_back_to_playwright_browser_without_system_browsers(sync):
    browser = object()
    launch = (Mock if sync else AsyncMock)(side_effect=[missing("chrome"), missing("msedge"), browser])
    assert run_launch(sync, launch, {"channel": "chrome"}) is browser
    assert [call.kwargs["channel"] for call in launch.call_args_list] == ["chrome", "msedge", "chromium"]


@pytest.mark.parametrize("sync", [False, True])
def test_uses_headless_shell_if_full_chromium_is_missing(sync):
    browser = object()
    launch = (Mock if sync else AsyncMock)(side_effect=[missing("chromium"), browser])
    assert run_launch(sync, launch, {"channel": "chromium"}, running_in_docker=True) is browser
    assert [call.kwargs["channel"] for call in launch.call_args_list] == ["chromium", None]


@pytest.mark.parametrize("sync", [False, True])
def test_all_missing_browsers_have_desktop_and_source_repair_instructions(sync):
    launch = (Mock if sync else AsyncMock)(side_effect=PlaywrightError("Executable doesn't exist at C:/missing"))
    with pytest.raises(launcher.BrowserLaunchError, match="完整版 Microsoft Edge") as caught:
        run_launch(sync, launch, {"channel": "chrome"})
    assert "python -m playwright install chromium" in str(caught.value)
    assert "WebView2" in str(caught.value)
    assert isinstance(caught.value.__cause__, PlaywrightError)
    assert [call.kwargs["channel"] for call in launch.call_args_list] == ["chrome", "msedge", "chromium", None]


@pytest.mark.parametrize("sync", [False, True])
def test_browser_crash_is_reported_without_hiding_it_by_switching_channels(sync):
    launch = (Mock if sync else AsyncMock)(side_effect=PlaywrightError("Access denied by browser policy"))
    with pytest.raises(launcher.BrowserLaunchError, match="Access denied by browser policy"):
        run_launch(sync, launch, {"channel": "chrome"})
    assert launch.call_count == 1


def test_cancelling_browser_start_does_not_try_another_browser():
    launch = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run_launch(False, launch, {"channel": "chrome"})
    launch.assert_awaited_once()
