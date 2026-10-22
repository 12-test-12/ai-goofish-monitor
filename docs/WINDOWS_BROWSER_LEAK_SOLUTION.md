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
- **防止同一任务重复启动拉起多个浏览器**：
  引入 `self._starting_task_ids: Set[int] = set()`，在 `start_task` 中实现启动过程状态互斥，杜绝由于手抖或调度重叠并发拉起多个相同任务的浏览器实例。

### 2. `src/scraper.py` 优化 `finally` 资源清理与移除阻塞 `input()`
- 彻底移除 `if debug_limit: input(...)` 及 5 秒多余延时。
- 在 `finally` 块中使用受保护的 `try...except` 确保无论任何异常或取消信号，`browser.close()` 都能百分百可靠执行，优先释放 Chromium 临时用户目录与内存。

---

## 三、 测试与验证结果

1. **进程管理单元测试（5 项全部通过）**：
   - `tests/unit/test_process_service.py`: 全部通过，包含新增的 Windows `taskkill /F /T` 树形终止调用验证。
2. **并发启动测试验证**：
   - `test_concurrent_starts_spawn_only_once`: 原先因并发冲突而失败，修改后成功通过并作为正式回归用例。
