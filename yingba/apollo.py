"""Official Apollo HTTP transport; disabled until explicitly configured.

This module performs no calls on import. Search is a potentially paid operation:
the caller must reserve its budget and persist an unknown-outcome marker BEFORE
calling search(authorized=True). A response is not a billing receipt. In
particular, documented prices and response headers never become actual_points.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import re
import ssl
from urllib.parse import urlencode

from .adapters import ConnectionPause, UnknownOutcome
from .normalize import domain

HOST = "api.apollo.io"
SEARCH_PATH = "/api/v1/mixed_companies/search"
HEALTH_PATH = "/api/v1/auth/health"
CREDIT_PATH = "/api/v1/usage_stats/credit_usage_stats"
MAX_RESPONSE = 4 * 1024 * 1024

# Supported subset of the official search filters. Other fields fail closed,
# preventing accidental enrichment, arbitrary endpoints, or ignored typos.
LIST_FILTERS = {
    "q_organization_domains_list": 1000,
    "organization_num_employees_ranges": 100,
    "organization_locations": 100,
    "organization_not_locations": 100,
    "organization_industry_tag_ids": 100,
    "organization_not_industry_tag_ids": 100,
    "organization_industries": 100,
    "organization_naics_codes": 100,
    "not_organization_naics_codes": 100,
    "organization_sic_codes": 100,
    "not_organization_sic_codes": 100,
    "q_organization_keyword_tags": 150,
    "q_not_organization_keyword_tags": 150,
    "organization_ids": 100,
    "not_organization_websites_list": 1000,
}
TEXT_FILTERS = {"q_organization_name"}


class ApolloPause(ConnectionPause):
    """Safe, user-facing refusal; a paid request still requires reconciliation."""

    def __init__(self, code: str, message: str, *, status=None, billing_unknown=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.billing_unknown = billing_unknown


class ApolloUnknown(UnknownOutcome):
    """No conclusion about charge or result may be inferred from this error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.billing_unknown = True


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} 必须为 {low} 至 {high} 的整数")
    return value


def _filter_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("Apollo 搜索条件必须是非空短文本")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Apollo 搜索条件不能包含控制字符")
    return value.strip()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def build_search_request(page: int, per_page: int = 100, filters: dict | None = None) -> dict:
    """Return a canonical, credential-free request suitable for durable hashing.

    Array filter order is irrelevant to Apollo search and is normalized. The
    selected filters and page size should remain fixed for a task's lifetime.
    """
    result = {
        "page": _integer(page, 1, 500, "Apollo 页码"),
        "per_page": _integer(per_page, 1, 100, "每页公司数"),
    }
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise ValueError("Apollo 搜索条件必须为字典")
    if any(not isinstance(key, str) for key in filters):
        raise ValueError("Apollo 搜索字段名必须为文本")
    for key, value in sorted(filters.items()):
        if key in LIST_FILTERS:
            if not isinstance(value, (list, tuple)) or len(value) > LIST_FILTERS[key]:
                raise ValueError("Apollo 数组搜索条件类型或数量不正确")
            if value:
                result[key] = sorted({_filter_text(item) for item in value})
        elif key in TEXT_FILTERS:
            result[key] = _filter_text(value)
        else:
            raise ValueError("不支持的 Apollo 搜索字段；请使用界面列出的条件")
    if len(result) == 2:
        raise ValueError("请至少指定一个 Apollo 搜索条件")
    return result


def request_fingerprint(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ApolloPage:
    records: list[dict]
    exhausted: bool
    terminal_reason: str
    pagination: dict
    raw: dict
    collected: str
    request_fingerprint: str
    provider_request_id: str | None = None

    def to_payload(self) -> dict:
        """Serializable response, explicitly NOT a PaidGateway Receipt."""
        return {
            "records": self.records,
            "exhausted": self.exhausted,
            "terminal_reason": self.terminal_reason,
            "pagination": self.pagination,
            "raw": self.raw,
            "collected": self.collected,
            "request_fingerprint": self.request_fingerprint,
            "provider_request_id": self.provider_request_id,
            "source_url": f"https://{HOST}{SEARCH_PATH}",
            "billing_status": "unconfirmed",
            "actual_points": None,
            "documented_estimate_points": 1,
            "billing_note": "官方当前套餐参考值，不是本账号实际扣费；旧套餐可能不同，需对账",
        }


def parse_search_response(raw: dict, request: dict, *, provider_request_id=None) -> ApolloPage:
    """Parse Apollo's documented organizations/pagination response shape.

    Malformed pagination is never treated as an empty, successful search.
    Organization records remain candidates, including incomplete records.
    """
    def invalid():
        raise ApolloUnknown("response_invalid", "Apollo 已返回响应但结构无法核对；已暂停，禁止重发该付费动作")

    if not isinstance(raw, dict) or raw.get("error") or raw.get("error_details") or raw.get("success") is False:
        invalid()
    rows, pagination = raw.get("organizations"), raw.get("pagination")
    if not isinstance(rows, list) or not isinstance(pagination, dict):
        invalid()
    if len(rows) > request["per_page"] or any(not isinstance(row, dict) for row in rows):
        invalid()
    if raw.get("accounts"):
        # Organization Search can include an accounts array. Its object contract
        # is not specified by this endpoint; never silently discard paid results.
        raise ApolloUnknown("account_schema_unverified", "Apollo 返回已保存账户记录，当前对象结构未核验；保留付费动作待对账，不重发")
    for key in ("page", "per_page", "total_entries", "total_pages"):
        if type(pagination.get(key)) is not int or pagination[key] < 0:
            invalid()
    if pagination["page"] != request["page"] or pagination["per_page"] != request["per_page"]:
        invalid()
    page, per_page = pagination["page"], pagination["per_page"]
    total, pages = pagination["total_entries"], pagination["total_pages"]
    if (total == 0 and (rows or pages > 1)) or (total > 0 and pages == 0):
        invalid()
    if not rows and total > (page - 1) * per_page and page <= pages:
        invalid()
    exhausted = total == 0 or page >= min(pages, 500)
    limited = page == 500 and pages > 500
    reason = "apollo_page_limit" if limited else ("search_exhausted" if exhausted else "")
    if raw.get("partial_results_only") is True and exhausted:
        reason = "apollo_partial_results_limit"
    records = []
    collected = datetime.now(timezone.utc).isoformat()
    for row in rows:
        def text_field(field):
            value = row.get(field)
            return value.strip() if isinstance(value, str) else ""
        records.append({
            "name": text_field("name"),
            "domain": domain(text_field("primary_domain") or text_field("website_url")),
            "apollo_id": text_field("id"),
            "website_url": text_field("website_url"),
            "apollo_raw": row,
            "source_url": f"https://{HOST}{SEARCH_PATH}",
            "collected": collected,
        })
    return ApolloPage(records, exhausted, reason, dict(pagination), raw, collected,
                      request_fingerprint(request), provider_request_id)


class ApolloClient:
    """Pinned HTTPS client with no retries, no redirects, and no API fallback.

    The API key is only retained in process memory and sent in x-api-key. It is
    excluded from repr, response objects, errors, URLs, and request fingerprints.
    Environment loading, if desired, is the caller's explicit responsibility.
    """

    def __init__(self, api_key: str | None = None, *, enabled=False, timeout=30):
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("Apollo 超时必须在 0 至 120 秒之间")
        self.enabled = enabled
        self.timeout = timeout
        self.__api_key = api_key

    def __repr__(self):
        return f"ApolloClient(enabled={self.enabled!r}, api_key=<redacted>)"

    def _require_ready(self):
        if not self.enabled:
            raise ApolloPause("disabled", "Apollo 未连接：接口尚未启用")
        key = self.__api_key
        if not isinstance(key, str) or not key or key != key.strip() or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ApolloPause("credential_missing", "Apollo 未连接：请在连接设置中填写有效 API key")

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self.__api_key, "[REDACTED]")
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()
                    if str(k).casefold().replace("-", "_") not in {"api_key", "x_api_key", "authorization", "access_token", "refresh_token"}}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _request(self, method: str, path: str, *, query=None, paid=False):
        self._require_ready()
        if (method, path) not in {("POST", SEARCH_PATH), ("GET", HEALTH_PATH), ("POST", CREDIT_PATH)}:
            raise ApolloPause("endpoint_refused", "Apollo 请求不在官方固定接口清单内")
        url_path = path
        if query:
            pairs = []
            for key, value in sorted(query.items()):
                if isinstance(value, list):
                    pairs.extend((key + "[]", item) for item in value)
                else:
                    pairs.append((key, value))
            url_path += "?" + urlencode(pairs)
        if len(url_path) > 32000:
            raise ValueError("Apollo 搜索条件过长，请减少筛选项")
        conn = None
        try:
            # http.client neither follows redirects nor consults proxy variables.
            conn = http.client.HTTPSConnection(HOST, timeout=self.timeout,
                                                context=ssl.create_default_context())
            conn.request(method, url_path, body=b"{}" if method == "POST" else None,
                         headers={"x-api-key": self.__api_key, "Accept": "application/json",
                                  "Content-Type": "application/json", "Cache-Control": "no-cache",
                                  "User-Agent": "YINGBA-Workbench/1"})
            response = conn.getresponse()
            status = response.status
            if status != 200:
                codes = {
                    401: ("authentication", "Apollo 登录或 API key 已失效，请重新连接"),
                    402: ("credits", "Apollo 额度不足，请核对账号；程序不会购买额度"),
                    403: ("permission", "Apollo 拒绝访问：请核对现有套餐及 API key 权限"),
                    429: ("rate_limit", "Apollo 达到请求频率或账号额度限制，已暂停"),
                    400: ("request_rejected", "Apollo 拒绝搜索参数，已暂停"),
                    422: ("request_rejected", "Apollo 搜索条件未通过校验，已暂停"),
                }
                if status in codes:
                    code, message = codes[status]
                    if paid:
                        message += "；该动作费用仍需对账，不能自动重试"
                    raise ApolloPause(code, message, status=status, billing_unknown=paid)
                if 300 <= status < 400:
                    raise ApolloPause("redirect_refused", "Apollo 返回跳转，已阻止凭据转发并暂停",
                                      status=status, billing_unknown=paid)
                if paid:
                    raise ApolloUnknown("http_unknown", "Apollo 服务器结果未知，已暂停；禁止盲目重试")
                raise ApolloPause("http_error", "Apollo 连接检查失败，请稍后手动检查", status=status)
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError("response too large")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/json", "text/json"}:
                raise ValueError("unexpected content type")
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_json_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            request_id = response.getheader("X-Request-Id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", request_id) or self.__api_key in request_id:
                request_id = None
            return self._redact(data), request_id
        except (ApolloPause, ApolloUnknown):
            raise
        except Exception:
            # Raw network/server messages can echo credentials. Never expose them.
            if paid:
                raise ApolloUnknown("network_or_response_unknown", "Apollo 付费请求结果未知，已暂停；先对账，禁止自动重发") from None
            raise ApolloPause("connection_failed", "Apollo 连接检查未成功，请检查网络、登录及接口权限") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def health(self) -> dict:
        """Explicit, zero-credit authentication check; not search authorization."""
        data, _ = self._request("GET", HEALTH_PATH)
        if data.get("healthy") is not True or data.get("is_logged_in") is not True:
            raise ApolloPause("authentication", "Apollo 未确认有效登录，请检查 API key")
        return {"healthy": True, "is_logged_in": True,
                "search_permission_verified": False, "paid_calls_verified": False}

    def credit_usage(self) -> dict:
        """Zero-credit billing-cycle snapshot, never a Beijing daily baseline.

        Team totals may be delayed and have multiple incompatible credit types;
        callers must neither sum them nor infer a per-request charge from them.
        """
        data, _ = self._request("POST", CREDIT_PATH)
        if not isinstance(data.get("credit_usage_stats"), dict) or not isinstance(data.get("current_credit_cycle"), dict):
            raise ApolloPause("credit_schema", "Apollo 账期余额结构未通过校验，不能作为预算确认")
        return {"raw": data, "scope": "team_billing_cycle", "daily_used": None,
                "collected": datetime.now(timezone.utc).isoformat()}

    def search(self, request: dict, *, authorized=False) -> ApolloPage:
        """Send exactly one paid request; caller owns the persisted budget guard.

        authorized=True means caller has already checked current authorization,
        reserved the account-wide budget, and persisted the unknown marker. It
        does not imply connection, successful billing, or a settled receipt.
        """
        self._require_ready()
        if authorized is not True:
            raise ApolloPause("paid_not_authorized", "Apollo 付费搜索未授权或预算未预留，未执行请求")
        if not isinstance(request, dict):
            raise ValueError("Apollo 请求必须为字典")
        filters = {k: v for k, v in request.items() if k not in {"page", "per_page"}}
        canonical = build_search_request(request.get("page"), request.get("per_page"), filters)
        if canonical != request:
            raise ValueError("请使用 build_search_request 固定并规范化请求后再预留预算")
        data, request_id = self._request("POST", SEARCH_PATH, query=canonical, paid=True)
        return parse_search_response(data, canonical, provider_request_id=request_id)
