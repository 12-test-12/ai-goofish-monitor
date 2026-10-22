# Windows 浏览器残留与内存泄露解决方案设计与实施文档

## 一、 问题根因剖析

在 Windows 环境下，导致 Edge/Chromium 浏览器进程残留和内存不断泄露的原因主要有以下 3 个关键点：

1. **Windows `process.terminate()` 无法终结子进程树**：
   - 原始代码：`src/services/process_service.py`
   - 机理：Playwright 在运行过程中，由 Python 主进程拉起 Node.js 驱动，再由驱动拉起多个 `msedge.exe` / `chromium.exe` 进程。
   - 当用户在 Web 页面点击“停止任务”或调度器停止任务时，原代码调用 `process.terminate()` 和 `process.kill()`。在 Windows 下，这仅向 Python 主进程发送 `TerminateProcess`，其派生的**所有浏览器子进程全部沦为孤儿进程（Orphan Processes）**，仍在后台潜伏运行，持续占用几百兆甚至几 G 内存。
2. **调试代码 `input()` 阻塞阻止了 `browser.close()` 执行**：
   - 原始代码：`src/scraper.py`
   - 机理：代码遗留了 `if debug_limit: input("按回车键关闭浏览器...")`。在 Web 启动或定时运行（无交互控制台）时，`input()` 会直接卡死线程或抛出 `EOFError`，导致其下方的 `await browser.close()` 永远无法被执行。
3. **`finally` 块硬编码 5 秒休眠拖慢退出**：
   - 原始代码：`src/scraper.py`
   - 机理：任务结束时无条件 `await asyncio.sleep(5)`，在任务被中止或需要快速释放资源时造成不必要的阻塞。

---

## 二、 实施落地方案

### 1. `src/services/process_service.py` 引入 Windows 进程树级联终止与启动防并发
- **实现异步 Windows 树形强杀**：
  在 `ProcessService` 中新增 `_kill_process_tree_windows(self, pid: int)`，使用 `asyncio.create_subprocess_exec("taskkill", "/F", "/T", "/PID", str(pid))`，不阻塞主事件循环，强制将 Python 进程及其下属的所有 Edge/Chromium 浏览器进程一并终结，防止产生孤儿进程。
  检查 `taskkill` 退出码，并为其等待设置超时；命令失败时回退终止任务主进程，超时时终止并回收 `taskkill` 自身。最终进程退出等待也有超时，停止失败时保留运行时记录供后续重试。回退只能处理主进程，不能保证子进程全部退出，需根据日志排查。
- **防止同一任务重复启动拉起多个浏览器**：
  引入 `self._starting_task_ids: Set[int] = set()`，在 `start_task` 中实现启动过程状态互斥，杜绝由于手抖或调度重叠并发拉起多个相同任务的浏览器实例。
  启动标记通过 `finally` 释放；请求取消时等待子进程创建完成，接管并停止已创建的进程，关闭日志句柄后继续传播取消信号，允许再次启动。

### 2. `src/scraper.py` 优化 `finally` 资源清理与移除阻塞 `input()`
- 彻底移除 `if debug_limit: input(...)` 及 5 秒多余延时。
- 启动浏览器后立即用异步上下文管理器接管关闭操作，覆盖上下文初始化、页面创建、后台分析收尾抛错和协程取消路径。关闭异常记录日志，不覆盖原始业务异常。
- 强制终止进程不会运行 Python 的清理逻辑；操作系统异常、重复取消及浏览器关闭失败时仍需结合日志排查，不能保证所有情形下资源均成功释放。

---

## 三、 测试与验证结果

1. **进程管理回归测试**：
   - `tests/unit/test_process_service.py`：覆盖进程退出、并发启动只创建一次、启动取消后清理及重试、Windows 树形终止调用、非零退出码、命令缺失、超时及最终等待上限。
2. **浏览器清理回归测试**：
   - `tests/unit/test_scraper_browser_cleanup.py`：覆盖上下文初始化、初始化脚本、页面创建、后台分析收尾失败，以及协程取消与关闭异常。
3. **执行方式与边界**：
   - `python -m pytest tests/unit/test_process_service.py tests/unit/test_scraper_browser_cleanup.py -s`（Windows 现有标准输出初始化会干扰 pytest 捕获，使用 `-s` 运行）。
   - 上述测试模拟浏览器和进程；实际 Windows 浏览器树清理仍应在真实任务中观察验证。
4. **2026-10-22 复核结果**：
   - 进程与浏览器清理相关 19 项测试全部通过；`tests/unit` 与 `tests/integration` 合计 138 项通过、3 项失败。
   - 失败为 `test_sanitize_no_proxy_handles_both_keys`、`test_sanitize_no_proxy_unwraps_bracketed_ipv6` 和 `test_save_to_jsonl`，均在未修改的 `46bb780` 隔离副本上复现，本次未修改这些模块。
