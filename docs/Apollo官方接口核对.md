# Apollo 官方接口核对与真实连接边界

核对日期：2026-10-23。此轮仅阅读公开官方文档并运行本地 HTTP 测试服务，**未访问真实 Apollo API、未读取真实密钥、未消费 Apollo 积分、未采集真实公司**。

## 已完成的实现

| 项目 | 代码与行为 | 验证方式 |
| --- | --- | --- |
| 公司搜索 HTTP | `yingba/apollo.py: ApolloClient.search`；真实 HTTPS 请求，固定 `api.apollo.io`，无模拟回退 | 本地服务接收实际 POST，核对路径、查询参数与头部 |
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

核对到的公司搜索契约没有承诺返回每次实际扣费、扣费归属日、可据以恢复该次动作的账单流水，或调用者可控制的搜索幂等键。不能由此断言 Apollo 所有产品永远没有这些能力；当前这条已核对接口不足以自动证明它们。

因此：

- `ApolloPage.to_payload()` 固定 `billing_status="unconfirmed"`、`actual_points=None`。响应中的可选 `X-Request-Id` 只是观察到的请求标识，可能不存在，不能伪造或当作收费回执。
- 总控应先持久化动作、请求指纹和预算预留，再标记结果未知，最后仅发送一次。成功的公司响应也应通过 `PaidGateway.Observation` 保存并暂停，待用户核对动作专属收费依据、实际积分、北京时间归属日后结算。不要将参考价格直接构造为 `Receipt`。
- HTTP 失败也不能自动认定扣费为零；断网、超时、进程被杀时同一动作禁止盲重发。重新登录不会自动清除待对账动作。
- 50 分是用户给出的全账号每日控制上限。程序本地账本能约束共用该账本的任务；账号若同时被其他工具、电脑或成员消费，需重新核实全账号当天用量。仅凭本程序数据库或账期余额无法保证外部并发消费的上限。
- 返回的记录只是候选；网站核实、邮箱归属、业务判定、历史去重和正式导出仍由后续阶段完成，Apollo 搜索结果本身不等于合格客户。

## 总控接入约定（供维护者）

```python
from yingba.apollo import ApolloClient, build_search_request
from yingba.paid import Observation

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
    return Observation(payload)  # 没有实际扣费回执，不能返回 Receipt。
```

任务一旦开始，应持久化其搜索条件与每页数量；以后分页必须沿用，不能改变同一动作的指纹。若需要不同条件，创建另一个明确的搜索轮次/任务。当前支持公司关键词、公司名、地区、行业、员工数区间、域名及 ID 等明确列出的筛选项；未知字段拒绝执行，不静默忽略。

`accounts` 非空时，官方公司搜索页面未给出该数组的完整对象契约，本适配器明确暂停结构核验，不静默丢弃这部分公司或把账户 ID 当作公司 ID。此限制可能需要未来按官方账户结构补充适配；不是提示用户自行编写代码。

## 本轮实际测试与剩余验收

运行命令：`python -m unittest tests.test_apollo_transport -v`。初次运行 25/25 通过，全部公司与 key 为显式测试数据。测试覆盖请求形态、分页/补页依据、超额保留、无结果、异常分页、500 页显示上限、部分结果限制、所有主要 HTTP 错误、断线未知结果、JSON 错误与重复键、响应体大小、认证拒绝、脱敏和健康检查不夸大连接状态。最终整体验收结果以交付包的第二轮测试报告为准。

尚未做真实调用验收，原因是本次任务明确禁止真实付费 Apollo 操作，也不允许使用用户已关机的电脑。这不是“只欠登录”：还需确认现有账号/API key 对公司搜索的权限、实际套餐计费、全账号当天预算基线，以及收费动作对账的可用依据。需要用户回来后从界面完成这些最小操作，程序不会自动购买任何服务。Windows TLS/网络代理环境也需要在用户本机验证。
