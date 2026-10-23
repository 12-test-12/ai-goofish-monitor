# Windows 桌面版

双击 `GoofishMonitor.exe` 即可打开独立桌面窗口，无需启动 Python 或浏览器。窗口支持缩放、最小化和最大化；顶部、登录页、任务栏和托盘使用统一的小鱼雷达图标。

关闭窗口会隐藏到系统托盘，监控和定时任务继续运行。单击托盘图标或选择“打开主窗口”恢复；选择“退出并停止监控”会关闭应用、停止调度和爬虫进程。若托盘未能初始化，关闭窗口会直接退出，避免程序隐藏后无法恢复。

## 下载

**[直接下载 EXE](https://github.com/12-test-12/ai-goofish-monitor/releases/latest/download/GoofishMonitor.exe)** · **[完整 ZIP 包](https://github.com/12-test-12/ai-goofish-monitor/releases/latest/download/GoofishMonitor-Windows-x64.zip)** · [全部 Releases](https://github.com/12-test-12/ai-goofish-monitor/releases)

ZIP 包含 EXE、使用说明、构建信息和 SHA256 校验值。解压后运行 `GoofishMonitor.exe`；升级前先从托盘退出旧版。

## 运行环境与数据

- 支持 Windows 10/11 x64，界面使用 Microsoft Edge WebView2 Runtime。通常系统已有该组件；缺少时请从 [Microsoft 官方页面](https://developer.microsoft.com/microsoft-edge/webview2/) 安装 Evergreen Runtime。它与抓取闲鱼所需的浏览器环境是两项独立要求。
- 默认数据目录为 `%LOCALAPPDATA%\GoofishMonitor`，保留旧桌面版的配置、账号和结果；可用 `GOOFISH_DATA_DIR` 指定其他绝对目录。
- 桌面版只监听 `127.0.0.1`，自动预留空闲端口。同一数据目录只允许一个桌面实例，重复打开时请从托盘恢复现有窗口。登录鉴权继续保留。
- 桌面日志位于数据目录的 `logs/desktop.log`；界面缓存位于 `desktop-profile/`，不要共享或提交该目录。不要在同一数据库上同时启动旧版和新版程序。
- `--no-browser` 保留原来的纯本地服务模式；`--browser` 使用外部浏览器。普通 Web/Docker 入口保持不变。
- 闲鱼登录、抓取和真实 AI 分析仍需配置对应账号和服务。程序未签名，当前交付为便携 EXE；不包含安装向导、自动更新或系统自启动。

## 本地构建

```powershell
python -m pip install -r requirements-desktop.txt
cd web-ui
npm ci
npm run build
cd ..
python scripts/ci/desktop_package.py record-frontend
python -m PyInstaller --noconfirm --distpath audit/desktop/dist --workpath audit/desktop/build desktop.spec
python scripts/ci/check_desktop_smoke.py audit/desktop/dist/GoofishMonitor.exe --native --report audit/desktop/gui.json
python scripts/ci/desktop_package.py prepare
```

打包只包含公开资源、默认提示词和 `.env.example`，不包含个人配置、密钥、登录状态或数据库。EXE 位于 `audit/desktop/dist/GoofishMonitor.exe`；可分发的文件整理到 `audit/desktop/package/`。整理脚本要求目标目录为空，重复构建时可通过 `--output` 指定新目录，避免混入旧文件。CI 默认使用当前 `GITHUB_SHA` 标识来源，本地运行默认标记为 `local`，可用 `--revision` 显式指定来源（记录前端和整理产物时应一致）。

## 自动化流水线

`.github/workflows/docker-image.yml` 在推送 master/main、`v*` 标签、对应分支的 PR 和手动触发时执行。Windows job 复用同一次运行中已通过构建的前端产物，在 Python 3.12 下运行离线测试、打包无控制台 EXE，并验证后台服务及真实桌面窗口、托盘和退出清理。

完整打包顺序：

1. Linux job 运行 `npm ci`、类型检查、Vite 构建和前端回归，然后记录 `frontend-build.json`，包含提交 SHA 与全部前端文件的 SHA256。
2. Windows job 下载 `frontend-dist`，核对提交 SHA 和文件校验值；缺少、修改、额外文件或提交不匹配都会阻断打包。
3. Windows 离线回归通过后，使用 `desktop.spec` 打包 EXE。
4. 启动 EXE 验证 worker、HTTP 鉴权、扫码 API 路由、真实窗口及托盘；打开打包后的“添加账号”窗口，确认默认提供“生成登录二维码”，避免回退到旧 JSON 界面。这一步不访问闲鱼，也不要求手机扫码。
5. 冒烟通过后，只将 EXE、`README.txt`、`build-info.json` 和 `SHA256SUMS.txt` 整理到上传目录；构建信息包含来源提交与前端构建标识。
6. 上传 `GoofishMonitor-Windows-x64`，在 Windows job 的 Summary 展示临时产物下载链接。
7. master/main 或 `v*` 标签的构建通过完整质量关卡后，自动创建 GitHub Release，附上 EXE、完整 ZIP、使用说明、构建信息及校验文件，并设为最新版本。首页下载入口始终指向最新 Release；Docker 仍按原有规则独立发布。

正式下载使用仓库首页或 Releases 的入口。测试 PR 构建时，可打开仓库 **Actions → CI, Desktop & Docker Build → 对应运行**，在 Artifacts 下载 `GoofishMonitor-Windows-x64` ZIP；也可使用 Windows job Summary 中的链接。Actions 构建包保留 30 天；Release 附件不受该期限限制。`windows-desktop-validation` 保存测试报告和扫码入口截图，保留 14 天。

PR 也会生成桌面构建产物，不发布 Release。只需发布桌面版时，可以在 master/main 手动运行工作流并取消勾选 `publish_docker`，桌面打包与 Release 发布仍会执行。分支构建的 Release 标签为 `desktop-<run_id>-<run_attempt>`，`v*` 构建使用原版本标签。只有提交并推送到远程后，GitHub 才会执行更新后的流水线；本地修改不会触发远程运行。

## 本次流水线验证（2026-10-23）

### Windows 健康检查失败排查

远程运行 `39954126875` 的 Windows 单元测试与 EXE 构建成功，冒烟启动阶段约 10 秒后报健康检查失败。质量关卡因此失败、Docker 发布被跳过属于依赖关系的预期结果。

纯服务模式现在与原生窗口模式一样，在导入应用前初始化无控制台日志；启动异常会写入数据目录的 `logs/desktop.log` 并以非零状态退出。冒烟脚本对回环地址禁用系统代理，启动等待默认上限为 120 秒，可通过 `--startup-timeout` 调整；检测到进程退出会立即失败，输出退出码、最近的请求错误与日志尾部。

失败时，只复制启动、worker 和窗口日志到报告同级的 `desktop-gui-logs/`，随后清理临时数据库、配置和界面缓存；不会复制账号或 `.env`。流水线已有的 `windows-desktop-validation` 会始终上传这些日志和 `failure.txt`，同时保存 `windows-dependencies.txt`，便于核对具体依赖版本。原生窗口测试超时也会清理整个进程树。

补充验证使用全新虚拟环境，与失败流水线相同的 FastAPI 0.161.1、Starlette 1.9.0 等依赖。368 项离线回归通过，包含无控制台异常记录、代理绕过、进程退出/超时和日志留存回归。旧入口在本地全新环境中也能通过 HTTP 冒烟，因此现有远程日志不足以确定原始退出原因；不能仅凭健康检查报错认定是依赖版本或启动慢。需要将修复提交推送后，在真实 Windows runner 上复验；再次失败时可从上述诊断产物取得原始异常。

最终修复版也已在该全新环境中实际打包，HTTP 鉴权、扫码 API、原生窗口、托盘和退出清理冒烟全部通过；本地证据位于 `audit/desktop-repro-fixed/reports/`。工作流 YAML 与新增步骤的 PowerShell 语法检查通过。

37 项相关 Python 回归、2 项前端回归、工作流 YAML 解析及 Summary 步骤的 PowerShell 语法检查通过。已实际执行前端构建标识记录与校验、PyInstaller 打包、EXE 的 HTTP/原生窗口/扫码入口冒烟及下载目录整理。新版 EXE 约 71.7 MiB，归档检查确认包含前端构建清单，未包含个人根目录配置或账号目录。

本地验证产物位于 `audit/desktop-pipeline-check/package/`，原生窗口报告与截图位于 `audit/desktop-pipeline-check/reports/`。这次验证不调用闲鱼或 AI 服务；远程 GitHub Actions 需要推送包含工作流和扫码功能的提交后再执行，本地通过不代表远程运行已经通过。

## 已有验证记录与界面

Windows / Python 3.12 的 319 项离线回归通过，3 项真实服务测试排除；前端类型检查、生产构建与 2 项前端回归通过。源代码原生窗口冒烟已验证登录、图标、托盘隐藏/恢复和退出时关闭后台服务。

PyInstaller 实际打包及最终无控制台 EXE 的同组冒烟均通过，产物约 71.5 MiB。流水线 YAML 已解析检查，远程 Actions 尚未触发。

![桌面工作区（隔离测试数据）](../web-ui/screenshots/desktop-native.png)

界面容器与主线程约束参考 [pywebview 文档](https://pywebview.flowrl.com/guide/usage.html)；Windows 渲染环境参考 [Web engine 文档](https://pywebview.flowrl.com/guide/web_engine.html)；托盘行为参考 [pystray 文档](https://pystray.readthedocs.io/en/latest/usage.html)。

## 图标来源

使用内置 imagegen 生成原创位图，源文件为 `assets/desktop/goofish.png`。`goofish.ico` 包含 16–256 像素多种尺寸；前端使用 `web-ui/public/app-icon.png`。格式转换保留透明通道。完整生成提示词见同目录 `ICON_PROMPT.md`。
