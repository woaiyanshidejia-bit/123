# Apollo 官方接口核对与真实连接边界

核对日期：2026-10-23。此轮仅阅读公开官方文档并运行本地 HTTP 测试服务，**未访问真实 Apollo API、未读取真实密钥、未消费 Apollo 积分、未采集真实公司**。

## 已完成的实现

| 项目 | 代码与行为 | 验证方式 |
| --- | --- | --- |
| 公司搜索 HTTP | `yingba/apollo.py: ApolloClient.search`；真实 HTTPS 请求，固定 `api.apollo.io`，无模拟回退 | 本地服务接收实际 POST，核对路径、查询参数与头部 |
| 已保存公司免费读取 | `ApolloClient.search_accounts`；固定 `POST /api/v1/accounts/search`，官方 JSON 请求体，独立免费来源 | 22 项本地 HTTP 与分页测试；不会触发 enrichment、创建账户或收费搜索 |
| 认证 | API key 仅放 `x-api-key` 请求头；实例默认关闭，显式授权且上层已原子预留预算才可搜索 | 未配置/未授权时连接函数调用数为零 |
| 请求与分页 | `build_search_request` 固定筛选字段、规范化数组、稳定请求指纹；每页 1–100，页码 1–500 | 两页贯通、最后一页、500 页截断、空结果、分页错配 |
| 原始响应 | `ApolloPage.to_payload` 返回原始对象、公司候选、采集时间、分页、请求指纹 | 原始字段回读、候选未自动标合格 |
| 错误处理 | 401/402/403/429、拒绝参数、跳转、服务器异常、断线和结构错误均暂停 | 每次情形仅一个 HTTP 请求，无自动重试 |
| 防泄漏 | 不记录 key，不含于 repr/URL/指纹；响应若意外回显当前 key 则脱敏，敏感字段删除 | 服务端故意回显测试密钥，输出中无法找到它 |
| 零积分健康检查 | `ApolloClient.health()`；仅在用户明确检查连接时调用 | 必须同时 `healthy=true` 和 `is_logged_in=true`；不宣称搜索权限验收成功 |
| 账期余额 | `ApolloClient.credit_usage()` 已实现只读接口 | 输出标记团队账期范围，`daily_used=None` |

不提供任意服务器地址、HTTP 降级、代理自动配置或自动跳转；生产 TLS 使用标准证书与主机名校验。测试仅替换底层连接到 `127.0.0.1`，不改变交付程序的官方域名限制。

## 官方文档确认的契约

1. 公司搜索为 `POST https://api.apollo.io/api/v1/mixed_companies/search`。筛选项与 `page`、`per_page` 使用文档列出的查询参数；数组按 `字段[]` 编码。返回 `organizations` 与 `pagination`，公司标识为 `id`，常见网站字段为 `primary_domain`、`website_url`。最多展示 50,000 条、每页最多 100 条、最多 500 页。文档列示当前套餐每页 1 积分。[Organization Search](https://docs.apollo.io/reference/organization-search)、[官方 OpenAPI 规范](https://docs.apollo.io/openapi/apollo-rest-api.json)
2. Apollo 用户使用已有 API key，放在 `x-api-key`；合作伙伴 OAuth 另有流程。本程序不将 API key 当作 Bearer token，不复制聊天插件认证。[Authentication](https://docs.apollo.io/reference/authentication)
3. `GET /api/v1/auth/health` 是官方测试 key 路径，正确响应包含两个布尔值。其验证只能证明当前 key 的基础认证状态，不能证明公司搜索权限、预算、扣费或数据正确性。[Test an API Key](https://docs.apollo.io/docs/test-api-key)
4. 官方价格说明指出旧套餐的计费可能不同，应核实账号实际规则。本程序只保留 `documented_estimate_points=1` 作为参考，不把它写入 `actual_points`。未列入收费表的接口不消耗积分；不执行购买、升级或增加额度。[API Pricing and Credits](https://docs.apollo.io/docs/api-pricing)
5. `POST /api/v1/usage_stats/credit_usage_stats` 为零积分只读接口，提供全团队**当前账期**各积分类型的额度、已用量、余额及账期日期，存在秒级更新延迟；统一积分池也不能把不同类型简单相加。它不提供北京时间当天消费的可靠明细。[View Credit Usage Stats](https://docs.apollo.io/reference/view-credit-usage-stats)
6. `usage_stats/api_usage_stats` 是 API 请求次数与频率限制统计，不是扣费账本；本程序不将请求计数充当积分余额。[View API Usage Stats and Rate Limits](https://docs.apollo.io/reference/view-api-usage-stats)

## 收费与恢复必须保持的边界

本节仅针对收费的 Organization Search（`mixed_companies/search`）。下文独立说明零积分的已保存公司读取，两条路径不可混用缓存或收费语义。

核对到的公司搜索契约没有承诺返回每次实际扣费、扣费归属日、可据以恢复该次动作的账单流水，或调用者可控制的搜索幂等键。不能由此断言 Apollo 所有产品永远没有这些能力；当前这条已核对接口不足以自动证明它们。

因此：

- `ApolloPage.to_payload()` 固定 `billing_status="unconfirmed"`、`actual_points=None`。响应中的可选 `X-Request-Id` 只是观察到的请求标识，可能不存在，不能伪造或当作收费回执。
- 总控应先持久化动作、请求指纹和预算预留，再标记结果未知，最后仅发送一次。v1.2.0 经 `CostPolicy` 核实官方与当前账号费率、本人当日授权和预算后，完整公司响应可通过 `BoundedObservation` 与可信解析校验进入 `observed_pending`，在保守预留范围内连续分页，实际费用保持 NULL。未知响应立即暂停；实扣核实后再单项或同额批次结算。不要将参考价格直接构造为 `Receipt`。
- HTTP 失败也不能自动认定扣费为零；断网、超时、进程被杀时同一动作禁止盲重发。重新登录不会自动清除待对账动作。
- 50 分是用户给出的全账号每日控制上限。程序本地账本能约束共用该账本的任务；账号若同时被其他工具、电脑或成员消费，需重新核实全账号当天用量。仅凭本程序数据库或账期余额无法保证外部并发消费的上限。
- 返回的记录只是候选；网站核实、邮箱归属、业务判定、历史去重和正式导出仍由后续阶段完成，Apollo 搜索结果本身不等于合格客户。

## 总控接入约定（供维护者）

```python
from yingba.apollo import ApolloClient, build_search_request, validate_bounded_search_payload
from yingba.paid import BoundedObservation

# key 来自用户当次在本机输入；不要写入设置文件、日志或数据库。
client = ApolloClient(key, enabled=True)
# 用户点击“检查连接”时可调用 client.health()；不会因此自动开始搜索。

request = build_search_request(
    page=1,
    per_page=100,
    filters={"q_organization_keyword_tags": ["lubricants", "lubricant distributor"]},
)

def transport(action, fixed_request):
    # 只能由已经原子预留预算并持久化 unknown 的 PaidGateway 调用。
    payload = client.search(fixed_request, authorized=True).to_payload()
    return BoundedObservation(payload)  # 仍不是实际扣费回执。
# 由Runtime传给execute_bounded，必须绑定CostPolicy产生的当日授权，
# response_validator调用validate_bounded_search_payload(payload,原query)。
```

任务一旦开始，应持久化其搜索条件与每页数量；以后分页必须沿用，不能改变同一动作的指纹。若需要不同条件，创建另一个明确的搜索轮次/任务。当前支持公司关键词、公司名、地区、行业、员工数区间、域名及 ID 等明确列出的筛选项；未知字段拒绝执行，不静默忽略。

收费公司搜索若收到非空 `accounts`，现已通过 `ApolloResponseCapture` 完整保留响应，不再在解析阶段抛错而丢失已收到的数据。有关混合分页的确切官方契约边界和恢复方式见下节；不会把已保存账户读取的分页规则套用于收费混合结果。

### 混合结果：保存证据与费用结算分开

2026-10-23 再次核对官方 OpenAPI：`paths./mixed_companies/search.post.responses.200.content.application/json.schema.properties.accounts` 仅声明数组，未定义 `items`；同一响应示例的 `accounts` 为空。文档没有说明非空 `accounts` 与 `organizations` 同时出现时，`pagination.total_entries` 和 `total_pages` 统计两者并集还是其他范围。独立 `/accounts/search` 虽定义账户字段，也不能据此推定另一个端点的混合分页语义。[官方 OpenAPI](https://docs.apollo.io/openapi/apollo-rest-api.json)、[Organization Search](https://docs.apollo.io/reference/organization-search)

当前实现对收到的混合结果做以下处理：

- 保存完整已脱敏 `raw`、来源、采集时间、请求指纹，以及两数组每条原始观察；不完整对象和非对象观察也不丢失。
- `review_candidates` 仅辅助离线核对已有字段。账户 ID、组织 ID 分开，未知字段留空，不猜官网。它不是正式公司清单，不能计入合格数量。
- 合计观察数量超过请求 `per_page` 时标记 `mixed_accounts_page_size_mismatch`；未超过时仍标记 `mixed_accounts_pagination_unverified`，不把数量合理当作官方语义确认。
- 返回 `response_state="schema_review_required"`、明确中文阻塞原因、`records=None`、`exhausted=None`。总控 `BoundedObservation` 验证失败或旧 `Observation` 流程把响应存入 `paid_observations` 后暂停。收到有效 JSON 但一般分页结构校验失败时，同样先保留响应。
- 进程重启后仍使用已保存证据；禁止重发原付费动作。费用与收费日期可以独立对账，但结算成功不会解除结构阻塞、推进页码或把候选计入达标。

恢复边界明确：需要 Apollo 对这条收费端点的混合分页契约给出依据，才能由维护方离线重解析已保存响应；不要求用户写代码，不需要为恢复再次发送原付费请求。当前没有把未确认的混合结果宣称为可正常推进的搜索页。已保存公司零积分读取与普通 `organizations` 搜索页不受该分支影响。

## 本轮实际测试与剩余验收

运行命令：`python -m unittest tests.test_apollo_transport -v`。初次运行 25/25 通过，全部公司与 key 为显式测试数据。测试覆盖请求形态、分页/补页依据、超额保留、无结果、异常分页、500 页显示上限、部分结果限制、所有主要 HTTP 错误、断线未知结果、JSON 错误与重复键、响应体大小、认证拒绝、脱敏和健康检查不夸大连接状态。本轮扩展了严格分页、免费读取、保守预留验证和贯通测试；最终整体验收结果以《第三轮验收报告》《测试报告》为准。

尚未做真实调用验收，原因是本次任务明确禁止真实付费 Apollo 操作，也不允许使用用户已关机的电脑。这不是“只欠登录”：还需确认现有账号/API key 对公司搜索的权限、实际套餐计费、全账号当天预算基线，以及收费动作对账的可用依据。需要用户回来后从界面完成这些最小操作，程序不会自动购买任何服务。Windows TLS/网络代理环境也需要在用户本机验证。

## 收敛方案 A：零积分读取已经保存的公司

此方案已实现，入口为 `build_accounts_request`、`ApolloClient.search_accounts`、`ApolloAccountsPage.to_payload`。它读取团队 Apollo 工作区**已经保存的公司账户**，可自动翻页，无需逐页填写收费回执；公司库搜索、解锁邮箱、付费 enrichment 均不会被该方法调用。没有已保存公司或读完仍不足目标时，应显示真实差额，不得自动改用收费接口。

官方 `Search for Accounts` 的费用栏为 **“0 credits”**；其说明为 **“This endpoint only returns accounts in the search results.”** 请求为 `POST https://api.apollo.io/api/v1/accounts/search`，筛选项是 JSON body：`q_organization_name`、`account_stage_ids`、`account_label_ids`、`sort_by_field`、`sort_ascending`、`page`、`per_page`。公司名筛选只匹配名称，不能宣称按业务关键词或行业搜索。查阅日期 2026-10-23，页面仅提供相对更新时间。[Search for Accounts](https://docs.apollo.io/reference/search-for-accounts)

```python
from yingba.apollo import ApolloClient, build_accounts_request

client = ApolloClient(key, enabled=True)
request = build_accounts_request(1, per_page=100, filters={})
# 空条件表示读取当前团队已保存的公司，不表示全库公司搜索。
payload = client.search_accounts(request).to_payload()
```

输出保留与原分页结构兼容的 `records`、`raw`、`pagination`、`collected`、请求指纹，并明确 `provenance="saved_accounts"`、`billing_status="documented_free"`、`actual_points=0`。这里的零值来自此独立只读端点的官方零积分契约，不是把收费公司搜索的预计值当作结算。缓存需按来源与动作隔离，不能仅凭相同字段的请求指纹与收费接口共用结果。

记录中的 `account_id` 来自账户对象 `id`，`organization_id` 单独保留；只有真实返回的组织 ID 才能进入公司 `apollo_id`。缺组织 ID 就留空，不能拿账户 ID 或人员 ID 替代。缺公司名、域名或其他字段的对象仍完整保留为候选及原始记录，不计入合格数量；不根据公司名称猜官网。已有账户也可能由用户手工创建，其存在不证明主体、业务或邮箱已核实。

权限与价格分开：官方零积分并不保证每个账号有权限。该端点接受具有 `api/v1/accounts/search` scope 的 key 或 master key；其官方 OpenAPI 仍有套餐限制的 403 示例，所以 Free/Basic 或旧套餐的实际可访问性必须以现有账号权限为准。遇到拒绝暂停，不换 master key、不升级套餐、不切收费接口。[官方 OpenAPI](https://docs.apollo.io/openapi/apollo-rest-api.json)

最终专项运行：`python -m unittest tests.test_apollo_transport tests.test_apollo_free`，**47/47 通过，0.728 秒**，其中已保存公司 18 项、收费传输与响应捕获 29 项。混合结果测试覆盖候选总数检查、ID 命名空间、不完整原始记录、脱敏、真实 `PaidGateway`/SQLite 持久化、关闭重开、不重发以及费用结算后仍保持结构阻塞。全部网络操作只到本地测试服务。未做真实账号的免费 API 请求，故仍不能显示真实连接已验收。

## 免费 People Search 调查结论：不接入当前收敛方案

查阅日期同为 2026-10-23。`POST /api/v1/mixed_people/api_search` 的官方费用栏为 **“0 credits”**，并明确 **“This endpoint doesn't return email addresses or phone numbers.”** 它支持人员与雇主地区、行业等筛选，`q_keywords` 的文档语义较宽，不能当作公司业务分类核验。[People API Search](https://docs.apollo.io/reference/people-api-search)

官方 OpenAPI 的 `people[].organization` 当前只保证公司名称与 `has_industry`、`has_city` 等可用性标记；没有承诺公司域名、官网或组织 ID。`people[].id` 是人员 ID。因此可以把返回的公司名称作为未经核实的线索，但不能直接当作已确认公司接到需要域名的核实流水线，也不能把 `has_email=true` 当作已获取邮箱。当前版本不接该路线、不猜域名、不暗中调用收费 enrichment。若将来接入，仍需另外实现有来源证据的公司名称消歧与官网发现，且不能承诺凑足 1000 家。[官方 OpenAPI](https://docs.apollo.io/openapi/apollo-rest-api.json)

## API key、套餐、费用和限流是四件不同的事

以下均于 2026-10-23 只读核对，未操作用户账号：

| 事项 | 官方短引文（每来源累计不超过 25 个英文单词） | 工程结论 |
| --- | --- | --- |
| 最小 key 权限 | “Give each key only the endpoints it actually needs.” | 普通 scoped key 可只授权实际端点；master key 不是所有搜索必需，也不能突破套餐权限。[Create an API Key](https://docs.apollo.io/docs/create-api-key) |
| Free/Basic 基本 API | “All pricing plans include at least basic access to the Apollo API” | 基本 API 不等于每个端点都能访问；创建 key 需管理员或相应 permission profile。Free 的某些数据库搜索要求工作邮箱及额外资格，付费账号不受该项 Free 条件影响。[Developer FAQs](https://docs.apollo.io/docs/developer-faqs) |
| 网站套餐描述 | “For more advanced or custom integrations, API access is available on Custom plans.” | 官网定价页与开发文档所描述的 API 层次不同；不能据 Basic/Free 标签保证某个用户的具体权限，也不能自动采购 Custom。[Pricing](https://www.apollo.io/pricing) |
| 旧套餐费用 | “If your workspace is on a legacy plan, API credit consumption may differ.” | 收费公司搜索仍须实际套餐与扣费核对。[API Pricing and Credits](https://docs.apollo.io/docs/api-pricing) |
| 团队限流 | “Per team, not per API key or per user.” | 限流是团队共用，不能通过多 key 绕过；固定相对时间窗口也不等于北京时间自然日。[Rate Limits](https://docs.apollo.io/reference/rate-limits) |
| 内部业务使用 | “solely for your internal business purposes” | 此处按用户自己的业务工作台设计；不是数据转售产品或外部客户共享授权。具体账号协议仍适用。[API Terms](https://www.apollo.io/terms/api)（页面明确更新日期 2024-08-13） |

当前官方普通端点限流表列 Free：50 次/分钟、200 次/小时、600 次/日；Basic：200 次/分钟、400 次/小时、2000 次/日。**已保存 Accounts Search 属于普通已保存记录读取**，不享受数据库 Search 类在付费套餐上的较高每小时/每日额度。后者当前表列 Free：50/200/600，Basic：200/6000/50000。旧套餐可不同，最终以端点返回与账号实际限额为准；这些均为请求频率，不是积分消费，也不是本工作台的 50 分预算。[Rate Limits](https://docs.apollo.io/reference/rate-limits)

允许使用已获授权 API 返回的公司字段作为内部线索，是基于官方接口用途与内部使用条款的工程判断，不是对所有合同或地区作法律保证。零积分不会解除权限限制；此次也没有通过官方接口以外方式抓取 Apollo、换用第三方凭据或绕过访问限制。
