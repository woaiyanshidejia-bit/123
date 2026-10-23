"""Official Apollo HTTP transport; disabled until explicitly configured.

This module performs no calls on import. Organization Search is potentially paid:
the caller must reserve its budget and persist an unknown-outcome marker BEFORE
calling search(authorized=True). A response is not a billing receipt. In
particular, its documented prices and response headers never become actual_points.
The separate Accounts Search endpoint reads already-saved accounts and is
documented as zero-credit; it never falls back to a paid search or enrichment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

from .adapters import ConnectionPause, UnknownOutcome
from .normalize import domain

HOST = "api.apollo.io"
SEARCH_PATH = "/api/v1/mixed_companies/search"
ACCOUNTS_PATH = "/api/v1/accounts/search"
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
ACCOUNT_LIST_FILTERS = {"account_stage_ids", "account_label_ids"}
ACCOUNT_SORT_FIELDS = {"account_last_activity_date", "account_created_at", "account_updated_at"}


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


def build_accounts_request(page: int, per_page: int = 100, filters: dict | None = None) -> dict:
    """Canonical official JSON body for the zero-credit saved-accounts search.

    Empty filters mean all already-saved accounts, not Apollo's company database.
    Account name matching is not an industry/business-activity search.
    """
    result = {"page": _integer(page, 1, 500, "Apollo 页码"),
              "per_page": _integer(per_page, 1, 100, "每页公司数")}
    if filters is None:
        filters = {}
    if not isinstance(filters, dict) or any(not isinstance(key, str) for key in filters):
        raise ValueError("已保存公司筛选条件必须为文本字段字典")
    for key, value in sorted(filters.items()):
        if key == "q_organization_name":
            result[key] = _filter_text(value)
        elif key in ACCOUNT_LIST_FILTERS:
            if not isinstance(value, (list, tuple)) or len(value) > 1000:
                raise ValueError("Apollo 已保存公司标签或阶段必须为有限 ID 数组")
            if value:
                result[key] = sorted({_filter_text(item) for item in value})
        elif key == "sort_by_field":
            if not isinstance(value, str) or value not in ACCOUNT_SORT_FIELDS:
                raise ValueError("不支持的 Apollo 已保存公司排序字段")
            result[key] = value
        elif key == "sort_ascending":
            if type(value) is not bool:
                raise ValueError("Apollo 排序方向必须为布尔值")
            result[key] = value
        else:
            raise ValueError("已保存公司读取不支持此字段，不会改用收费搜索")
    if "sort_ascending" in result and "sort_by_field" not in result:
        raise ValueError("指定排序方向时必须同时指定排序字段")
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


@dataclass(frozen=True)
class ApolloAccountsPage(ApolloPage):
    """A documented-free saved-account result, not a global database search."""

    def to_payload(self) -> dict:
        payload = super().to_payload()
        payload.update({
            "provenance": "saved_accounts",
            "source_url": f"https://{HOST}{ACCOUNTS_PATH}",
            "billing_status": "documented_free",
            "actual_points": 0,
            "documented_estimate_points": 0,
            "billing_note": "官方列示零积分，只读已有公司；仍须现有账号具备端点权限",
        })
        return payload


@dataclass(frozen=True)
class ApolloResponseCapture:
    """An observed JSON response requiring schema review, never a usable page.

    Keep received paid results recoverable through the existing Observation
    store. None deliberately fails the runtime's list/bool page contract, even
    if billing is subsequently reconciled. This object never implies success.
    """
    raw: dict
    request: dict
    blocking_code: str
    blocking_reason: str
    provider_request_id: str | None = None
    collected: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_payload(self) -> dict:
        review_candidates = []
        for source_array in ("organizations", "accounts"):
            rows = self.raw.get(source_array)
            if not isinstance(rows, list):
                continue
            for row in rows:
                # Provisional field views aid offline review; they are not
                # released to Engine as company records until contract review.
                view = {"source_array": source_array, "raw_record": row}
                if isinstance(row, dict):
                    def text_field(field):
                        value = row.get(field)
                        return value.strip() if isinstance(value, str) else ""
                    account = source_array == "accounts"
                    organization_id = text_field("organization_id") if account else text_field("id")
                    view.update({
                        "name": text_field("name"),
                        "domain": domain(text_field("domain") if account else
                                         (text_field("primary_domain") or text_field("website_url"))),
                        "account_id": text_field("id") if account else "",
                        "organization_id": organization_id,
                        "apollo_id": organization_id,
                    })
                else:
                    view["parse_problem"] = "row_not_object"
                review_candidates.append(view)
        return {
            "response_state": "schema_review_required",
            "blocking_code": self.blocking_code,
            "blocking_reason": self.blocking_reason,
            "blocking_message": self.blocking_reason,
            "records": None,
            "exhausted": None,
            "pagination_verified": False,
            "pagination": self.raw.get("pagination"),
            "raw": self.raw,
            "review_candidates": review_candidates,
            "observed_candidate_count": len(review_candidates),
            "requested_per_page": self.request["per_page"],
            "collected": self.collected,
            "request_fingerprint": request_fingerprint(self.request),
            "provider_request_id": self.provider_request_id,
            "source_url": f"https://{HOST}{SEARCH_PATH}",
            "billing_status": "unconfirmed",
            "actual_points": None,
            "documented_estimate_points": 1,
            "billing_note": "已收到的响应须先保存；费用对账与响应结构验收分别处理，禁止重发原动作",
        }


def parse_accounts_response(raw: dict, request: dict, *, provider_request_id=None) -> ApolloAccountsPage:
    """Keep all account candidates, with account/organization IDs kept separate."""
    def invalid():
        raise ApolloPause("accounts_response_invalid", "Apollo 已保存公司响应结构或分页无法核对，已暂停")

    if not isinstance(raw, dict) or raw.get("error") or raw.get("error_details") or raw.get("success") is False:
        invalid()
    rows, pagination = raw.get("accounts"), raw.get("pagination")
    if not isinstance(rows, list) or not isinstance(pagination, dict):
        invalid()
    if len(rows) > request["per_page"] or any(not isinstance(row, dict) for row in rows):
        invalid()
    if raw.get("organizations"):
        invalid()
    for key in ("page", "per_page", "total_entries", "total_pages"):
        if type(pagination.get(key)) is not int or pagination[key] < 0:
            invalid()
    if pagination["page"] != request["page"] or pagination["per_page"] != request["per_page"]:
        invalid()
    page, per_page = pagination["page"], pagination["per_page"]
    total, pages = pagination["total_entries"], pagination["total_pages"]
    # An explicit total must agree with every page and its observed row count.
    # Otherwise e.g. total=1000/pages=1 could silently stop after two rows.
    # A short middle page or a partial-results marker is not proof of exhaustion.
    if raw.get("partial_results_only", False) is not False:
        invalid()
    expected_pages = (total + per_page - 1) // per_page
    if total == 0:
        if page != 1 or pages not in (0, 1) or rows:
            invalid()
    elif pages != expected_pages or page > pages:
        invalid()
    expected_rows = min(per_page, max(0, total - (page - 1) * per_page))
    if len(rows) != expected_rows:
        invalid()
    exhausted = total == 0 or page >= min(pages, 500)
    reason = "apollo_page_limit" if page == 500 and pages > 500 else ("saved_accounts_exhausted" if exhausted else "")
    records = []
    collected = datetime.now(timezone.utc).isoformat()
    for row in rows:
        def text_field(field):
            value = row.get(field)
            return value.strip() if isinstance(value, str) else ""
        organization_id = text_field("organization_id")
        records.append({
            "name": text_field("name"),
            "domain": domain(text_field("domain")),
            "account_id": text_field("id"),
            "organization_id": organization_id,
            "apollo_id": organization_id,
            "website_url": text_field("website_url"),
            "provenance": "saved_accounts",
            "apollo_raw": row,
            "source_url": f"https://{HOST}{ACCOUNTS_PATH}",
            "collected": collected,
        })
    return ApolloAccountsPage(records, exhausted, reason, dict(pagination), raw, collected,
                              request_fingerprint(request), provider_request_id)


def parse_search_response(raw: dict, request: dict, *, provider_request_id=None) -> ApolloPage | ApolloResponseCapture:
    """Parse Apollo's documented organizations/pagination response shape.

    Malformed pagination is never treated as an empty, successful search.
    Organization records remain candidates, including incomplete records.
    """
    def invalid():
        raise ApolloUnknown("response_invalid", "Apollo 已返回响应但结构无法核对；已暂停，禁止重发该付费动作")

    if not isinstance(raw, dict) or raw.get("error") or raw.get("error_details") or raw.get("success") is False:
        invalid()
    accounts = raw.get("accounts", [])
    if not isinstance(accounts, list):
        return ApolloResponseCapture(raw, request, "mixed_accounts_structure_invalid",
            "Apollo 返回的 accounts 不是已核对的数组；原始响应已接收，须保存并核对结构，禁止重发", provider_request_id)
    if accounts:
        # The Organization Search OpenAPI gives accounts only {type: array};
        # unlike Accounts Search, neither items nor mixed pagination is defined.
        # Count ALL observations conservatively, but do not infer page meaning.
        organizations = raw.get("organizations")
        observed_count = len(accounts) + (len(organizations) if isinstance(organizations, list) else 0)
        oversized = observed_count > request["per_page"]
        code = "mixed_accounts_page_size_mismatch" if oversized else "mixed_accounts_pagination_unverified"
        reason = ("Apollo 两个数组的候选总数超过请求每页上限；" if oversized else "Apollo 混合公司/账户的分页统计语义未获官方契约确认；")
        return ApolloResponseCapture(raw, request, code,
            reason + "原始响应须保存待结构核对，费用结算后仍不推进页码或计入达标，禁止重发", provider_request_id)
    rows, pagination = raw.get("organizations"), raw.get("pagination")
    if not isinstance(rows, list) or not isinstance(pagination, dict):
        invalid()
    if len(rows) > request["per_page"] or any(not isinstance(row, dict) for row in rows):
        invalid()
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
    if rows and (page > pages or total < (page - 1) * per_page + len(rows)):
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


def validate_bounded_search_payload(payload, request) -> bool:
    """Fail-closed predicate for a complete Organization Search observation.

    Reparse the raw response bound to the canonical original query, then compare
    the entire generated payload rather than trusting a records/list flag. This
    verifies consistency, not provider authenticity or an actual billing charge:
    transport TLS and the gateway's durable response digest still supply those
    respective transport/storage boundaries. No network calls occur here.
    """
    try:
        if not isinstance(payload, dict) or not isinstance(request, dict):
            return False
        filters = {key: value for key, value in request.items() if key not in {"page", "per_page"}}
        canonical = build_search_request(request.get("page"), request.get("per_page"), filters)
        if canonical != request:
            return False
        if payload.get("request_fingerprint") != request_fingerprint(canonical):
            return False
        if payload.get("source_url") != f"https://{HOST}{SEARCH_PATH}":
            return False
        if payload.get("billing_status") != "unconfirmed" or payload.get("actual_points") is not None:
            return False
        if "response_state" in payload or type(payload.get("exhausted")) is not bool:
            return False
        if not isinstance(payload.get("records"), list) or not isinstance(payload.get("pagination"), dict):
            return False
        collected = payload.get("collected")
        if not isinstance(collected, str):
            return False
        timestamp = datetime.fromisoformat(collected)
        if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
            return False
        request_id = payload.get("provider_request_id")
        if request_id is not None and (not isinstance(request_id, str) or
                re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", request_id) is None):
            return False
        parsed = parse_search_response(payload.get("raw"), canonical, provider_request_id=request_id)
        if type(parsed) is not ApolloPage:
            return False
        # Bounded automatic continuation requires an internally complete page.
        # A partial-result marker, short intermediate page, or contradictory
        # total/page count must remain observable but cannot authorize progress.
        if parsed.raw.get("partial_results_only", False) is not False:
            return False
        pagination = parsed.pagination
        total, pages = pagination["total_entries"], pagination["total_pages"]
        page, per_page = pagination["page"], pagination["per_page"]
        if total:
            if pages != (total + per_page - 1) // per_page or page > pages:
                return False
        elif page != 1 or pages not in (0, 1):
            return False
        expected_count = min(per_page, max(0, total - (page - 1) * per_page))
        if len(parsed.records) != expected_count:
            return False
        expected = parsed.to_payload()
        expected["collected"] = collected
        for record in expected["records"]:
            record["collected"] = collected
        # JSON comparison distinguishes booleans from integers and rejects
        # nonfinite numbers; exact keys also reject injected success metadata.
        encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False)
        return encode(payload) == encode(expected)
    except Exception:
        # Invalid or adversarial payloads must neither authorize progress nor
        # leak the raw contents through exception diagnostics.
        return False


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

    def _request(self, method: str, path: str, *, query=None, paid=False, json_body=None):
        self._require_ready()
        if (method, path) not in {("POST", SEARCH_PATH), ("POST", ACCOUNTS_PATH), ("GET", HEALTH_PATH), ("POST", CREDIT_PATH)}:
            raise ApolloPause("endpoint_refused", "Apollo 请求不在官方固定接口清单内")
        if json_body is not None and (method, path) != ("POST", ACCOUNTS_PATH):
            raise ApolloPause("body_refused", "Apollo 请求体不属于已核对的只读接口")
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
        encoded_body = b"{}" if method == "POST" else None
        if json_body is not None:
            encoded_body = json.dumps(json_body, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(encoded_body) > 32000:
                raise ValueError("Apollo 已保存公司筛选条件过长，请减少筛选项")
        # One wall-clock budget covers DNS, TCP/TLS, response headers and body.
        # Socket inactivity timeouts alone do not stop a slow byte stream.
        deadline = time.monotonic() + self.timeout
        expired = threading.Event()
        conn = None
        watchdog = None
        active_sockets = []

        def remaining():
            value = deadline - time.monotonic()
            if expired.is_set() or value <= 0:
                raise TimeoutError('Apollo request deadline exceeded')
            return value

        def expire():
            expired.set()
            # getresponse() may hand ownership to HTTPResponse and clear
            # conn.sock; retained references still interrupt its makefile reads.
            sockets = list(active_sockets)
            current = getattr(conn, 'sock', None)
            if current is not None:
                sockets.append(current)
            for current in sockets:
                try:
                    current.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass
            if conn is not None:
                try:
                    conn.close()
                except (OSError, ValueError):
                    pass

        def bounded_connection(address, timeout=None, source_address=None):
            # Preserve HTTPSConnection's original host/SNI and certificate
            # verification. Only its TCP creation seam uses the resolved IP.
            from .public_web import PublicFetcher
            remaining()
            records = PublicFetcher(resolver=socket.getaddrinfo).resolve(address[0], address[1], deadline)
            remaining()  # A delayed DNS worker must never trigger a later send.
            if not records:
                raise OSError('empty DNS result')
            family, kind, protocol, _, target = records[0]
            if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
                raise OSError('unsupported DNS result')
            numeric = ipaddress.ip_address(target[0])
            if (numeric.version == 4) != (family == socket.AF_INET):
                raise OSError('inconsistent DNS address')
            # Direct numeric connect avoids a second unbounded getaddrinfo call
            # inside socket.create_connection after the bounded DNS lookup.
            sock = socket.socket(family, kind, protocol)
            active_sockets.append(sock)
            try:
                sock.settimeout(remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(target)
                sock.settimeout(remaining())  # TLS receives only time left.
                return sock
            except BaseException:
                sock.close()
                raise
        try:
            # http.client neither follows redirects nor consults proxy variables.
            conn = http.client.HTTPSConnection(HOST, timeout=self.timeout,
                                                context=ssl.create_default_context())
            conn._create_connection = bounded_connection
            watchdog = threading.Timer(remaining(), expire)
            watchdog.daemon = True
            watchdog.start()
            remaining()
            conn.request(method, url_path, body=encoded_body,
                         headers={"x-api-key": self.__api_key, "Accept": "application/json",
                                  "Content-Type": "application/json", "Cache-Control": "no-cache",
                                  "User-Agent": "YINGBA-Workbench/1"})
            if conn.sock is not None:
                active_sockets.append(conn.sock)
            remaining()
            response = conn.getresponse()
            remaining()
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
            chunks, size = [], 0
            while True:
                remaining()
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                remaining()
                if not chunk:
                    if response.length not in (None, 0):
                        raise ValueError('incomplete response')
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError("response too large")
            body = b''.join(chunks)
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
            redacted = self._redact(data)
            remaining()
            return redacted, request_id
        except (ApolloPause, ApolloUnknown):
            raise
        except Exception:
            # Raw network/server messages can echo credentials. Never expose them.
            if paid:
                raise ApolloUnknown("network_or_response_unknown", "Apollo 付费请求结果未知，已暂停；先对账，禁止自动重发") from None
            raise ApolloPause("connection_failed", "Apollo 连接检查未成功，请检查网络、登录及接口权限") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
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

    def search(self, request: dict, *, authorized=False) -> ApolloPage | ApolloResponseCapture:
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
        try:
            return parse_search_response(data, canonical, provider_request_id=request_id)
        except ApolloUnknown as error:
            # A valid received JSON body must not disappear because schema
            # validation failed. The existing Observation gateway persists it.
            return ApolloResponseCapture(data, canonical, error.code,
                "Apollo 响应已收到但结构校验未通过；保存原始响应核对，不能推进任务或重发", request_id)

    def search_accounts(self, request: dict) -> ApolloAccountsPage:
        """Read one zero-credit page of already-saved accounts; never enrich.

        Enabling the client is still explicit. The caller persists page responses
        and treats authentication/permission/rate-limit failures as pauses.
        """
        self._require_ready()
        if not isinstance(request, dict):
            raise ValueError("Apollo 已保存公司请求必须为字典")
        filters = {k: v for k, v in request.items() if k not in {"page", "per_page"}}
        canonical = build_accounts_request(request.get("page"), request.get("per_page"), filters)
        if canonical != request:
            raise ValueError("请使用 build_accounts_request 固定并规范化请求")
        data, request_id = self._request("POST", ACCOUNTS_PATH, json_body=canonical, paid=False)
        return parse_accounts_response(data, canonical, provider_request_id=request_id)
