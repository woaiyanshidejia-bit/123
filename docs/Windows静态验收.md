# Windows 交付第二轮验收

检查日期：2026-10-23。开发与检查均在云端 Linux 完成，没有使用用户电脑、Windows 浏览器或用户认证凭据。

**结论：启动包通过本轮云端静态检查与离线依赖测试；Windows 10 双击、官方 CLI 安装和本人登录仍需实机验收。** 本报告不代表真实 Apollo 或真实 Codex 订阅采集已通过验收。

## 已修复的实际问题

| 问题 | 修改及位置 | 可证明的范围 |
| --- | --- | --- |
| 目录切换失败仍继续，UNC 路径不支持 `cd /d` | 四个根目录 `.cmd` 改为带引号的 `pushd "%~dp0"`，立即检测失败，结束时 `popd` | 静态检查保护分支；UNC 的 Windows 行为待实测 |
| 继承的延迟变量扩展会影响含 `!` 的目录 | 四个脚本首先 `setlocal EnableExtensions DisableDelayedExpansion` | 防止脚本路径被延迟扩展；路径表达式静态检查通过 |
| 仅寻找 Python Launcher，遗漏 PATH 中的 Python | `安装.cmd` 先探测 `py -3.12`，再探测 `python`，两者均检查精确 3.12 与 64 位 | 缺依赖时显示中文说明，不自动下载 Python |
| 依赖版本锁定但没有文件完整性校验 | `requirements.lock` 锁定两只随包 wheel 的 SHA256；安装加 `--isolated --no-index --no-cache-dir --force-reinstall --require-hashes` | 实际离线安装成功；错哈希、缺 wheel 被回归测试拒绝 |
| npm 的 `codex.cmd` 从另一个 `.cmd` 直接启动后无法可靠返回 | `登录Codex.cmd` 改为 `call codex -c forced_login_method=chatgpt login` | 命令使用官方 CLI 配置语法；没有运行真实登录 |
| `pause` 覆盖启动/诊断退出状态 | 调用后立即保存 `%ERRORLEVEL%`，退出时返回保存的值 | 静态校验覆盖启动、诊断、登录三个入口 |
| 中文脚本编码和 Git 换行可能不一致 | `.cmd` 全部 UTF-8 无 BOM、CRLF；`.gitattributes` 固定 CRLF，标记 wheel/ZIP/XLSX 为二进制 | 逐字节校验通过 |

登录脚本遇到 `OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_API_BASE`、`AZURE_OPENAI_API_KEY`、`CODEX_MODEL_PROVIDER` 或 `CODEX_ACCESS_TOKEN` 配置时停止，不输出变量值。不读取、复制、导出用户认证文件。CLI 认证和应用适配器是否通过验收仍是两项独立状态。

## 实际执行证据

1. `python -m unittest discover -s tests -p 'test_windows_launchers.py' -v`：**12 项通过**。包含正常脚本、UTF-8 BOM、LF 换行、延迟扩展、目录错误门控、缺 `call`、API 环境保护缺失、非精确版本、wheel 篡改、缺传递依赖和联网安装回归。
2. `python scripts/check_windows_package.py --report artifacts/windows-static-report.json`：**通过**。报告包含四个脚本、路径表达式、两只 wheel 的 SHA256 和平台标签。
3. 云端实际创建 `中文工作台 空格 ! 100% & (OEM)^` 目录，在其中创建新 venv，仅从随包 wheels 按锁定哈希安装依赖。退出码 **0**；`openpyxl 3.1.5` 安装成功；中文 SQLite 文件写读和中文 XLSX 保存/回读均通过。过程没有请求 PyPI 或安装额外依赖。
4. 静态路径样例覆盖普通盘符、中文/空格、`! % & ( ) ^`、UNC。检查仅展开受引号保护的目录表达式，没有模拟完整 CMD 解析器，也没有把 Linux 的文件路径测试当作 Windows 执行结果。

报告文件：`artifacts/windows-static-report.json`。完整源码包还包含 `scripts/check_windows_package.py` 和 `tests/test_windows_launchers.py`，可重复执行。根目录打包检查负责新 ZIP 的解包、清单、安装和全套测试；本报告与其相互补充。

随包运行依赖只有 `openpyxl==3.1.5` 和其传递依赖 `et-xmlfile==2.0.0`，均为不带平台二进制的通用 wheel。Python 3.12 64 位运行时本身、官方 Codex CLI 及其安装所需运行时不包含在本包中。

## 用户回来后的最少操作

1. 将 ZIP **完整解压到本地可写目录**，例如 `D:\YINGBA`。已有 Python 3.12 64 位时直接双击 `安装.cmd`；未安装时先从 Python 官方 Windows 下载页安装一次，并勾选 Add Python to PATH。安装脚本也兼容已安装的 `py -3.12` 启动器。
2. 双击 `启动.cmd`。先运行“模拟验收”确认网页、暂停/继续、导出和重启；模拟记录会保留明确标记。发生错误时双击 `诊断.cmd`，保留错误类型，避免发送凭据。
3. 只有需要检查订阅登录时，先按官方文档安装 Codex CLI，再双击 `登录Codex.cmd`，由本人在浏览器完成 ChatGPT 登录。工作台显示“已登录”也不能替代真实集成验收；连接未验收或计费条件不明确时应继续保持阻塞。

无需编写应用代码或自行拼接适配器。未满足真实接口与计费验收条件前，不启动真实采集，更不进行本次明确禁止的付费 Apollo 操作。

## 仍未验证的边界

- Windows 10 实际 `cmd.exe`、中文控制台字体与编码、Python 安装方式、Windows 单实例锁、默认浏览器自动打开、端口占用时的具体交互。
- 在 Windows 用户目录包含中文/空格/特殊字符时，官方 npm CLI 的完整执行链和浏览器回调。当前仅验证命令形态；没有浏览器登录或付费模型调用。
- UNC 目录只做目录表达式与官方命令语义检查；数据目录建议放在本地磁盘，未验收网络共享上的 SQLite 锁和崩溃恢复。
- 本报告不验收真实 Apollo 权限、定价、付款回执，也不保证某个 ChatGPT 套餐在运行时仍有有效权限或额度。

## 依据

- [Microsoft `call` 文档](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/call)：批处理调用另一批处理并返回；适用 Windows 10。
- [Microsoft `pushd` 文档](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/pushd)：启用扩展时处理盘符或网络目录，并可用 `popd` 恢复。
- 云端实际读取 `codex --help`：`-c key=value` 先按 TOML 解析，解析失败时将原文作为字面字符串。因此 `forced_login_method=chatgpt` 无需 CMD 中的嵌套引号；未执行登录。
- [Codex 官方认证文档](https://developers.openai.com/codex/auth)、[官方 CLI 安装入口](https://developers.openai.com/codex/cli/)。
