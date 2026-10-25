"""Shared browser selection for monitoring and QR login on portable installs."""

from playwright.async_api import Error as PlaywrightError


class BrowserLaunchError(RuntimeError):
    """Local browser setup failed; rotating accounts or proxies cannot fix it."""


def browser_channels(preferred: str | None, *, running_in_docker: bool = False):
    if running_in_docker:
        return ["chromium", None]
    return list(dict.fromkeys([preferred, "msedge", "chrome", "chromium", None]))


def _browser_name(channel):
    return {"chrome": "Chrome", "msedge": "Edge", "chromium": "Chromium", None: "Playwright 默认浏览器"}[channel]


def is_missing_browser_error(error: Exception | str) -> bool:
    message = str(error).lower()
    return (
        "executable doesn't exist" in message
        or "executable does not exist" in message
        or ("distribution" in message and "is not found" in message)
    )


def _unavailable_error() -> BrowserLaunchError:
    return BrowserLaunchError(
        "未找到可用的抓取浏览器（Chrome / Edge / Playwright Chromium）。"
        "桌面 EXE 用户请安装完整版 Microsoft Edge 或 Google Chrome 后重新运行任务；"
        "仅有 WebView2 或浏览器快捷方式不够。"
        "源码用户也可在运行程序的 Python 环境执行 python -m playwright install chromium。"
        "无需更换闲鱼账号或代理。"
    )


async def launch_browser(playwright, launch_kwargs, *, running_in_docker: bool = False):
    options = dict(launch_kwargs)
    preferred = options.pop("channel", None)
    channels = browser_channels(preferred, running_in_docker=running_in_docker)
    for index, channel in enumerate(channels):
        try:
            return await playwright.chromium.launch(**options, channel=channel)
        except PlaywrightError as error:
            if not is_missing_browser_error(error):
                raise BrowserLaunchError(f"{_browser_name(channel)} 启动失败: {error}") from error
            if index + 1 == len(channels):
                raise _unavailable_error() from error
            print(f"未找到 {_browser_name(channel)}，尝试 {_browser_name(channels[index + 1])}...")


def launch_browser_sync(playwright, launch_kwargs, *, running_in_docker: bool = False):
    options = dict(launch_kwargs)
    preferred = options.pop("channel", None)
    channels = browser_channels(preferred, running_in_docker=running_in_docker)
    for index, channel in enumerate(channels):
        try:
            return playwright.chromium.launch(**options, channel=channel)
        except PlaywrightError as error:
            if not is_missing_browser_error(error):
                raise BrowserLaunchError(f"{_browser_name(channel)} 启动失败: {error}") from error
            if index + 1 == len(channels):
                raise _unavailable_error() from error
            print(f"未找到 {_browser_name(channel)}，尝试 {_browser_name(channels[index + 1])}...")
