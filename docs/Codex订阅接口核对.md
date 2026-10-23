# Codex 订阅接口核对与验收边界

核对日期：2026-10-23。云端 CLI：`codex-cli 0.163.0`。

本版已实现官方 CLI 的进程调用、能力探测、本机固定样本验收、持久验收记录、JSON Schema 审核输出及引用二次校验。云端仅运行 CLI 帮助/版本/功能列表和本地假 CLI 子进程；**未执行真实模型请求、未登录用户账号、未读取或复制认证文件**。这不是已通过真实订阅集成的声明。

## 官方依据

| 核对项 | 官方接口与结论 |
| --- | --- |
| 订阅登录 | [Authentication](https://developers.openai.com/codex/auth)：`codex login` 是浏览器 ChatGPT 登录路径；`codex login status` 可查看登录方式。ChatGPT 订阅和 API 按量付费是不同方式。 |
| 固定登录方式 | [Configuration reference](https://developers.openai.com/codex/config-reference)：`forced_login_method="chatgpt"` 限制登录方式；同时固定内置 `model_provider="openai"`。不调用登录 token/API 接口。 |
| 自动化与结构化结果 | [Non-interactive mode](https://developers.openai.com/codex/noninteractive)：`codex exec` 复用 CLI 登录，`--json` 输出事件流，`--output-schema` 约束最终 JSON，`--output-last-message` 保存最终结果。 |
| 受限能力 | 云端 `codex exec --help` 确认 `--ignore-user-config`、`--ignore-rules`、`--ephemeral`、`--strict-config`；`codex features list` 确认本版关闭的 24 个能力名称确实存在。 |
| 工具边界 | [Hooks](https://learn.chatgpt.com/docs/hooks)：工具 hooks 不能覆盖所有托管/特殊工具路径，不能当成完整隔离边界。本程序不绕过 hook 信任，也不依赖提示词或 hooks 当作安全沙箱。 |

本地参数依据也已查阅官方 [CLI 源码配置 schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json)。不存在“聊天里的 Apollo 插件会自动出现在 CLI”这一假设；MCP 连接先只读枚举，然后本次审核逐个禁用。

## Windows 首次操作（不需要编程）

1. 先使用交付包的 `安装.cmd` 安装工作台。Codex CLI 是另外一个官方组件，不包含在 Python 安装包里。
2. 打开 Windows 开始菜单，输入 **PowerShell**，在普通用户窗口粘贴下面这一行，安装官方 Codex CLI。安装脚本来源与 [OpenAI 官方仓库安装说明](https://github.com/openai/codex#installing-and-running-codex-cli) 一致；此处未添加修改执行策略的选项。

   ```powershell
   irm https://chatgpt.com/codex/install.ps1 | iex
   ```

   若已安装 Node.js，也可使用官方支持的 npm 路径：`npm install -g @openai/codex`，二选一。组织策略拒绝安装时保持暂停，不降低组织策略。安装完关闭 PowerShell 窗口，重新打开工作台，让新 PATH 生效。

3. 双击交付包 `登录Codex.cmd`，在本人浏览器中完成 **ChatGPT** 登录。这个脚本使用官方 `codex -c forced_login_method=chatgpt login` 路径，不要求 API key，不要求复制认证文件。
4. 双击 `启动.cmd`，进入“连接状态”，运行只读诊断。然后点击 **运行订阅最小验收**；这一步会使用现有订阅额度，只发送固定合成样本，不搜索真实客户、不购买额度。完成后再配置 Apollo 的真实授权与预算，先执行最小真实验收。

Windows 支持不是无条件保证：本次核对的 [官方 Windows 支持矩阵](https://developers.openai.com/codex/windows) 推荐 Windows 11；更新完整的 Windows 10 是 best effort，实际要求 1809 或更新版本。Windows 10 的原生沙箱可能需要本人/管理员完成官方初始化；设备策略拒绝时暂停。MXC 是有特定系统要求的 Windows 11 能力，不能假定用户 Windows 10 已具备。本次未使用 Windows 电脑，没有把 Linux 测试写成 Windows 真机通过。

## 可直接接入的代码接口

`yingba/adapters.py` 的 `CodexSubscription` 是应用入口。`yingba/codex_bridge.py` 实现完整协议，`tests/test_codex_subscription.py` 提供实际子进程测试。

```python
bridge = CodexSubscription(receipt_path=data_dir / 'codex-acceptance.json')
state = bridge.preflight()                # 无模型请求；Apollo 付费动作前检查
# 以下只能由本人点击“执行订阅验收”按钮触发，会使用既有订阅额度。
state = bridge.validate_local(enabled=True)
result = bridge.review_company(record, evidence, enabled=True)
```

`record` 含 `name`、`domain`；`evidence` 为 `[{"id":"1","url":"https://公司域名/contact","text":"已抓取原文"}]`。模型不主动抓网页。字段 `identity_confirmed`、`business_confirmed`、`category`、`identity_refs`、`business_refs`、`email_refs`、`reason`、`company_domain` 有固定 Schema。引用必须包含 `evidence_id` 和逐字原文 `quote`；邮箱引用再含 `email`。返回值增加 `emails`、`basis`、`simulation=False`、`delivery_verified=False`。

`evaluate()` 保留函数名，但拒绝执行任意提示词，以免跳过固定证据合同。调用方应使用 `review_company()`；没有有效结构化审核正文的 `turn.completed` 不算成功。

## 验收门实际检查什么

1. 只接受 CLI 明确返回 `Logged in using ChatGPT`，拒绝 API 登录、含混文本、未知登录输出、非零退出码和超时。不会显示原始登录输出。
2. 发现 `OPENAI_API_KEY`、`CODEX_API_KEY`、API 基地址、Codex access token、自定义供应商等环境配置即暂停。子进程仅继承操作系统路径、语言、CLI home 等必要环境，不继承其它应用密码或代理凭据；不读取认证文件。
3. 必须支持结构化、严格配置、临时会话和配置隔离参数；必须存在所需功能关闭项；MCP 枚举无法解析也暂停。不存在静默降级。
4. 本人点击订阅验收后，用 `yingba-acceptance.example` 的固定合成材料调用真实 CLI。它不访问该网址，不搜索 Apollo，不采集客户。只有身份、业务、邮箱及引用合同通过，才原子保存验收记录。
5. 记录只含 CLI/命令/MCP 名称清单指纹、协议版本、时间和验收成功状态；没有认证信息、账号标识或网页内容。记录 24 小时有效，CLI 文件/版本或 MCP 清单变化需要重新验收。调用失败或工具事件会使记录失效。
6. 审核用临时空目录、只读沙箱、关闭主要执行/浏览/应用/插件/子代理/记忆/代码模式等能力、禁用已枚举 MCP、忽略项目说明、禁止审批提升并关闭无限重试。不设置危险绕过参数，不自动购买额度，不回退 API。
7. 结果同时必须满足：退出码成功、合法 JSONL、完整结束、最终消息与结果文件一致、固定 Schema、当前公司域名一致、引用存在于当前公司原文、邮箱完整 token 存在且同域。任意工具事件或未知事件拒收结果。

## 仍需诚实保留的边界

- 本机固定样本验收验证的是当前版本实际受限调用和输出合同，**不是所有未来工具路径都不存在的形式化证明**。只读沙箱限制写入，不等于整个个人目录不可读；程序通过关闭相关能力、清理环境和不给模型主动检索任务减小接触面。系统/企业管理策略仍由官方 CLI 执行，策略拒绝时暂停，不修改或绕过。
- CLI 升级可能改变功能、事件或登录输出，严格检查会保持未连接并提示重新验收。本次没有在 Windows 上确认官方沙箱的实际工作状态，也没有使用本人浏览器登录。
- 逐字引用证明来源一致，不能单独证明语义判断正确。业务资格仍必须与普通代码的公开网页审核相交，不能凭模型裸布尔值把公司计为合格；跨域邮箱、免费邮箱、无主体依据和不明确业务保持不合格或待核实。
- 超时、额度受限、权限拒绝、登录失效、未知结果均暂停，不自动重试模型。公开邮箱证据不是 SMTP 投递验证。

## 本次云端测试证据

命令：`python -m unittest tests.test_codex_subscription -v`，**28 项全部通过（6.114 秒）**。所有模型结果来自测试生成的本地 `fake codex.py`，测试通过 `subprocess.Popen` 真正启动该脚本，没有网络模型调用。另以本云真实 `codex features list` 核对 24 个必需关闭项，缺失项为 0；该命令不请求模型。

覆盖：订阅状态精确匹配、环境拒绝、旧 CLI/缺失能力/MCP 异常、显式验收、跨进程持久记录、版本和配置变化/过期失效、无正文伪完成、JSON 重复字段/类型错误/额外字段、结果文件缺失或不一致、工具事件/失败事件、超时/大输出/额度/权限/登录错误及脱敏、跨公司证据/伪造引用/邮箱 token 前后缀攻击、网页指令数据化、中文空格及含 `&` 的 Windows npm 路径解析、失败验收与审核失败使旧记录失效。

Windows 启动优先原生 `codex.exe`；标准 npm `codex.cmd` 则解析同目录官方包的 `node_modules/@openai/codex/bin/codex.js` 并直接交给 Node，避免 shell 拼接。云端已测解析规则，Windows 实际子进程、浏览器登录和官方沙箱仍待用户回来后用按钮验收，不需要用户改代码。
