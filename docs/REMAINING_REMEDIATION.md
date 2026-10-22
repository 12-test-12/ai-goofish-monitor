# 剩余审计项实施与当前边界

后续更新：本文记录首轮实施结果。通知持久重试、分批落盘导出、脏数据隔离、任务删除保护、仪表盘任务隔离和桌面启动时序已在[后续整改记录](FOLLOWUP_REMEDIATION_2026-10-22.md)中补齐；以下首轮边界按当时状态保留。

日期：2026-10-22。起始基线：`ca3bd2c`。本轮核对既有审计清单，补齐此前未覆盖的七组代码问题。

| 审计项 | 已实现行为 |
| --- | --- |
| A06 标准文件串用/误删 | 创建任务和修改需求都生成带随机标识的独立文件，排他创建，避免覆盖旧文件。生成或创建失败只清理本次新文件；任务已保存而调度刷新失败时保留引用文件，并报告已创建及调度提示。 |
| A07 去重和通知 | 结果保存、历史去重均按 task ID 隔离。同关键词任务可以各自保存同一商品；重复 INSERT 返回 false，只有成功落盘的新结果才通知。后台保存异常会在 join 时上抛，不再因任务提前结束而丢失。 |
| A08 下载阻塞 | 请求头、流式正文、文件写入全部在工作线程中完成。响应关闭后原子替换最终文件，失败清理 `.part`；取消时等待当前下载线程退出，再释放缓存占用锁。 |
| A10 CSV 公式注入 | 导出的每个字符串单元格统一检查 `= + - @`、制表符/换行等前缀，危险值前加单引号，避免作为公式解释。数字单元格保持数字。 |
| A16 查询内存 | 结果列表通过 SQL COUNT / LIMIT / OFFSET 分页；仪表盘摘要在 SQL 中聚合；可见商品 ID 只读取 ID。价格历史默认按最近 30 天窗口查询，趋势先在 SQL 中保留每天每件商品的最新观察；当前页价格说明仅读取相关商品及可见市场最新批次。 |
| F05 临时图片清理 | 服务启动和每小时巡检过期缓存，默认 24 小时。爬虫在 Web、CLI、桌面进程中共用跨进程占用锁，活跃任务不会被巡检删除；异常退出和取消也执行当前任务目录清理。仅处理新版本生成的摘要命名直接子目录，跳过符号链接、目录联接、嵌套内容和旧版任意名称目录。 |
| A15 桌面进程与资源 | 单文件程序区分界面服务和 `--spider-worker`；后端启动爬虫时执行同一 EXE 的子命令，并显式设置日志路径。数据保存在持久目录，前端从打包资源读取；仅复制默认提示词和环境示例，已有设置不覆盖。提供可复现打包配置和隔离冒烟脚本。 |

## 数据兼容与配置

- SQLite 自动把结果表唯一键升级为 `(result_filename, task_id, link_unique_key)`，保留原主键、原始 JSON、隐藏状态及查询索引。旧版无法确认归属的记录保存在 `task_id=-1` 的历史作用域，继续显示；各新任务首次运行会独立分析一次，不借用不确定的旧去重记录。迁移必须在所有旧版本进程停止后进行，升级后不要混跑旧爬虫。
- 结果列表仍按关键词汇总，同行商品可能来自不同任务，记录包含任务 ID 和任务名称。通知采用“新结果写入成功后尝试发送”，失败会记录日志；本轮没有实现持久通知队列和投递重试。
- 原有价格历史保留在数据库中，没有自动删除旧快照。默认分析窗口为最近一次观察前 30 天（保留原来按整天计算的边界），内部接口传 `window_days=None` 仍可读取全部历史。CSV/原始结果全文导出仍会按请求构造完整文件，不能视为任意规模的流式导出。
- `IMAGE_CACHE_MAX_AGE_HOURS=24`；设为 `0` 可禁用过期巡检。缓存锁位于数据库目录的 `image-leases/`。Web 和 CLI 应共用工作目录及 `APP_DATABASE_FILE`。
- 桌面默认数据目录为 `%LOCALAPPDATA%\GoofishMonitor`；可通过环境变量 `GOOFISH_DATA_DIR` 指定绝对路径。首次启动复制 `.env.example` 为 `.env`，以及两份默认提示词。用户账号、密钥和已生成标准不会打入 EXE。使用本机 Edge 抓取时应配置 `LOGIN_IS_EDGE=true`；本次 EXE 包含 Playwright 驱动，不附带 Chromium 浏览器。

## 本地验证与打包

```powershell
python -m pytest --ignore=tests/audit -m "not live and not live_slow" -q
cd web-ui
npm run build
cd ..
python -m pip install -r requirements-desktop.txt
python -m PyInstaller --noconfirm --distpath audit/desktop/dist --workpath audit/desktop/build desktop.spec
python scripts/ci/check_desktop_smoke.py audit/desktop/dist/GoofishMonitor.exe
```

冒烟脚本在独立临时数据目录中测试爬虫帮助子命令、配置跨启动保留、服务器健康、打包前端及资源、未登录拒绝和登录后 API 访问；不调用闲鱼或 AI 服务。退出时只终止该脚本启动的进程树。

本次验证结果：Windows / Python 3.12 **282 项离线回归通过，3 项 live 测试排除**；Vue 类型检查与生产构建通过；PyInstaller 6.22.3 实际构建和上述 EXE 冒烟通过。另检查压缩包目录，未嵌入 `.env`、`config.json`、`xianyu_state.json` 或 `app.sqlite3`。产物位于 `audit/desktop/dist/GoofishMonitor.exe`（不纳入 Git）。

本轮不代表完成整份产品路线图：真实账号长期运行、真实闲鱼/AI 联调、安装包签名与自动更新，以及自适应调度/单浏览器页面池仍需要独立实施和验收。
