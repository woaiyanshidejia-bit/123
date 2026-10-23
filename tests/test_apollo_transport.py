"""Official-shaped fixtures over loopback only; no real Apollo/API key access."""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from yingba.apollo import (ApolloClient, ApolloPause, ApolloUnknown, ApolloResponseCapture, HOST, SEARCH_PATH,
                          HEALTH_PATH, CREDIT_PATH, MAX_RESPONSE, build_search_request,
                          parse_search_response, request_fingerprint)

FAKE_KEY = "fixture-only-not-a-real-key"


def response(page=1, per_page=2, total=3, names=None):
    """Synthetic .example records with the official OpenAPI response structure."""
    if names is None:
        names = [f"模拟润滑油公司 {i}" for i in range((page - 1) * per_page, min(page * per_page, total))]
    return {"breadcrumbs": [], "partial_results_only": False, "accounts": [],
            "pagination": {"page": page, "per_page": per_page,
                           "total_entries": total, "total_pages": (total + per_page - 1) // per_page},
            "organizations": [{"id": f"simulation-{page}-{n}", "name": name,
                               "primary_domain": f"company{page}{n}.example",
                               "website_url": f"https://www.company{page}{n}.example/",
                               "phone": "SIMULATED"} for n, name in enumerate(names)]}


class ApolloTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reply = response()
        self.status = 200
        self.content_type = "application/json"
        self.extra_headers = {"X-Request-Id": "fixture-provider-request-1"}
        self.disconnect = False
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                size = int(self.headers.get("Content-Length", "0"))
                case.calls.append({"method": self.command, "path": self.path,
                                   "headers": dict(self.headers), "body": self.rfile.read(size)})
                if case.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = case.reply if isinstance(case.reply, bytes) else json.dumps(case.reply, ensure_ascii=False).encode()
                self.send_response(case.status)
                self.send_header("Content-Type", case.content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in case.extra_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

        def local_connection(host, *, timeout, context):
            self.assertEqual(host, HOST)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            return http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=timeout)

        self.patch = patch("yingba.apollo.http.client.HTTPSConnection", side_effect=local_connection)
        self.connection = self.patch.start()
        self.client = ApolloClient(FAKE_KEY, enabled=True, timeout=2)
        self.request = build_search_request(1, 2, {"q_organization_keyword_tags": ["润滑油", "lubricants"],
                                                  "organization_locations": ["Japan"]})

    def tearDown(self):
        self.patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def search(self):
        return self.client.search(self.request, authorized=True)

    def test_actual_loopback_http_request_matches_official_method_path_and_auth(self):
        page = self.search()
        call = self.calls[0]
        self.assertEqual(call["method"], "POST")
        url = urlsplit(call["path"])
        self.assertEqual(url.path, SEARCH_PATH)
        query = parse_qs(url.query)
        self.assertEqual(query["page"], ["1"])
        self.assertEqual(query["per_page"], ["2"])
        self.assertEqual(query["q_organization_keyword_tags[]"], ["lubricants", "润滑油"])
        self.assertEqual(query["organization_locations[]"], ["Japan"])
        self.assertEqual(call["headers"]["x-api-key"], FAKE_KEY)
        self.assertNotIn("Authorization", call["headers"])
        self.assertNotIn(FAKE_KEY, call["path"])
        self.assertEqual(json.loads(call["body"]), {})
        self.assertEqual(len(page.records), 2)
        self.assertFalse(page.exhausted)
        self.assertEqual(page.records[0]["domain"], "company10.example")
        self.assertEqual(page.records[0]["apollo_id"], "simulation-1-0")
        self.assertEqual(page.records[0]["apollo_raw"]["phone"], "SIMULATED")
        self.assertNotIn("qualified", page.records[0])

    def test_second_page_uses_same_filters_and_preserves_extra_valid_records(self):
        first = self.search()
        self.request = build_search_request(2, 2, {k: v for k, v in self.request.items() if k not in {"page", "per_page"}})
        self.reply = response(page=2)
        second = self.search()
        self.assertEqual(len(first.records) + len(second.records), 3)
        self.assertTrue(second.exhausted)
        self.assertEqual(second.terminal_reason, "search_exhausted")
        self.assertNotEqual(first.request_fingerprint, second.request_fingerprint)
        self.assertEqual(parse_qs(urlsplit(self.calls[1]["path"]).query)["page"], ["2"])

    def test_response_never_fabricates_paid_gateway_receipt(self):
        payload = self.search().to_payload()
        self.assertEqual(payload["billing_status"], "unconfirmed")
        self.assertIsNone(payload["actual_points"])
        self.assertEqual(payload["documented_estimate_points"], 1)
        self.assertEqual(payload["provider_request_id"], "fixture-provider-request-1")
        self.assertEqual(payload["raw"], self.reply)
        self.assertEqual(payload["request_fingerprint"], request_fingerprint(self.request))
        self.assertNotIn("external_reference", payload)
        self.assertNotIn("billed_day", payload)

    def test_no_request_id_is_not_locally_invented(self):
        self.extra_headers = {}
        self.assertIsNone(self.search().provider_request_id)

    def test_disabled_missing_key_and_unapproved_search_do_not_open_socket(self):
        for client in (ApolloClient(FAKE_KEY), ApolloClient(None, enabled=True)):
            with self.assertRaises(ApolloPause):
                client.search(self.request, authorized=True)
        with self.assertRaises(ApolloPause) as caught:
            self.client.search(self.request)
        self.assertEqual(caught.exception.code, "paid_not_authorized")
        self.assertFalse(self.connection.called)
        self.assertFalse(self.calls)

    def test_malformed_credential_and_invalid_config_are_not_logged_or_sent(self):
        for key in (" " + FAKE_KEY, FAKE_KEY + "\nInjected: value", "中文密钥", "", 123):
            with self.assertRaises(ApolloPause) as caught:
                ApolloClient(key, enabled=True).health()
            self.assertNotIn(FAKE_KEY, str(caught.exception))
        for timeout in (float("nan"), float("inf"), 0, 121, True):
            with self.assertRaises(ValueError):
                ApolloClient(FAKE_KEY, timeout=timeout)
        with self.assertRaises(ValueError):
            ApolloClient(FAKE_KEY, enabled="true")
        self.assertNotIn(FAKE_KEY, repr(self.client))
        self.assertFalse(self.connection.called)

    def test_health_is_zero_credit_endpoint_and_does_not_claim_search_permission(self):
        self.reply = {"healthy": True, "is_logged_in": True}
        result = self.client.health()
        self.assertEqual(self.calls[0]["method"], "GET")
        self.assertEqual(self.calls[0]["path"], HEALTH_PATH)
        self.assertFalse(result["search_permission_verified"])
        self.assertFalse(result["paid_calls_verified"])

    def test_health_false_is_disconnected_even_http_200(self):
        for raw in ({"healthy": True, "is_logged_in": False}, {"healthy": "true", "is_logged_in": True}, {}):
            self.reply = raw
            with self.assertRaises(ApolloPause):
                self.client.health()

    def test_credit_snapshot_is_explicitly_not_daily_usage(self):
        self.reply = {"credit_usage_stats": {"lead_credit": {"limit": 1000, "consumed": 26, "left_over": 974}},
                      "current_credit_cycle": {"start_date": "2026-10-01", "end_date": "2026-11-01"}}
        result = self.client.credit_usage()
        self.assertEqual(self.calls[0]["path"], CREDIT_PATH)
        self.assertEqual(result["scope"], "team_billing_cycle")
        self.assertIsNone(result["daily_used"])
        self.assertEqual(result["raw"], self.reply)

    def test_credit_snapshot_invalid_schema_refuses_baseline(self):
        self.reply = {"consumed": 26}
        with self.assertRaises(ApolloPause):
            self.client.credit_usage()

    def test_auth_permission_quota_and_rate_limit_pause_without_retry_or_leak(self):
        for status, code in ((401, "authentication"), (402, "credits"), (403, "permission"),
                             (429, "rate_limit"), (400, "request_rejected"), (422, "request_rejected")):
            with self.subTest(status=status):
                before = len(self.calls)
                self.status = status
                self.reply = {"error": f"reflected secret {FAKE_KEY}"}
                with self.assertRaises(ApolloPause) as caught:
                    self.search()
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.billing_unknown)
                self.assertEqual(len(self.calls), before + 1)
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_redirect_never_follows_or_forwards_key(self):
        self.status = 302
        self.extra_headers = {"Location": "http://127.0.0.1:1/steal"}
        with self.assertRaises(ApolloPause) as caught:
            self.search()
        self.assertEqual(caught.exception.code, "redirect_refused")
        self.assertTrue(caught.exception.billing_unknown)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_server_result_is_not_retried(self):
        self.status = 503
        with self.assertRaises(ApolloUnknown):
            self.search()
        self.assertEqual(len(self.calls), 1)

    def test_connection_drops_after_receiving_request_no_blind_retry(self):
        self.disconnect = True
        with self.assertRaises(ApolloUnknown):
            self.search()
        self.assertEqual(len(self.calls), 1)

    def test_bad_content_json_and_nonfinite_values_remain_unknown(self):
        scenarios = [(b"{broken", "application/json"), (b"{}", "text/html"),
                     (b"[]", "application/json"), (b'{"n": NaN}', "application/json"),
                     (b'{"error": "bad", "error": null}', "application/json")]
        for body, content_type in scenarios:
            with self.subTest(body=body):
                before = len(self.calls)
                self.reply, self.content_type = body, content_type
                with self.assertRaises(ApolloUnknown):
                    self.search()
                self.assertEqual(len(self.calls), before + 1)

    def test_overlarge_response_is_rejected_and_not_retried(self):
        self.reply = b" " * (MAX_RESPONSE + 1)
        with self.assertRaises(ApolloUnknown):
            self.search()
        self.assertEqual(len(self.calls), 1)

    def test_echoed_credentials_are_redacted_from_raw_data_and_ids(self):
        self.reply["organizations"][0]["description"] = "echo " + FAKE_KEY
        self.reply["api_key"] = FAKE_KEY
        self.reply["organizations"][0]["access_token"] = "another provider secret"
        self.extra_headers["X-Request-Id"] = FAKE_KEY
        payload = self.search().to_payload()
        saved = json.dumps(payload)
        self.assertNotIn(FAKE_KEY, saved)
        self.assertNotIn("another provider secret", saved)
        self.assertIn("[REDACTED]", saved)
        self.assertIsNone(payload["provider_request_id"])

    def test_arbitrary_endpoint_is_refused_before_transport(self):
        with self.assertRaises(ApolloPause):
            self.client._request("POST", "https://attacker.example/reveal")
        self.assertFalse(self.connection.called)

    def test_canonical_filters_are_order_independent_and_contain_no_credentials(self):
        a = build_search_request(1, 100, {"q_organization_keyword_tags": ["oem", "lubricant", "oem"]})
        b = build_search_request(1, 100, {"q_organization_keyword_tags": ["lubricant", "oem"]})
        self.assertEqual(a, b)
        self.assertEqual(request_fingerprint(a), request_fingerprint(b))
        self.assertNotIn(FAKE_KEY, json.dumps(a))

    def test_invalid_search_requests_never_reach_network(self):
        for page, per_page in ((0, 2), (501, 2), (True, 2), (1, 0), (1, 101), (1, True)):
            with self.assertRaises(ValueError):
                build_search_request(page, per_page, {"q_organization_name": "lube"})
        bad_filters = ({}, {"api_key": FAKE_KEY}, {"endpoint": "https://evil.example"},
                       {"q_organization_keyword_tags": "lube"}, {"q_organization_name": "a\nb"},
                       {"q_organization_keyword_tags": ["a"] * 151})
        for filters in bad_filters:
            with self.assertRaises(ValueError):
                build_search_request(1, 2, filters)
        bad = copy.deepcopy(self.request)
        bad["q_organization_keyword_tags"] = ["润滑油", "lubricants"]
        with self.assertRaises(ValueError):
            self.client.search(bad, authorized=True)
        self.assertFalse(self.connection.called)

    def test_bad_pagination_cannot_claim_empty_success(self):
        cases = []
        for key, value in (("page", 2), ("per_page", 3), ("total_entries", -1), ("total_entries", 1), ("total_pages", 0), ("page", True)):
            raw = response()
            raw["pagination"][key] = value
            cases.append(raw)
        raw = response(); raw["organizations"] = []; cases.append(raw)
        raw = response(); raw["organizations"].append({}); cases.append(raw)
        raw = response(); raw["success"] = False; cases.append(raw)
        raw = response(); raw["error_details"] = {"code": "CREDITS.UNKNOWN"}; cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ApolloUnknown):
                    parse_search_response(raw, self.request)

    def test_valid_empty_page_is_exhausted_and_unqualified(self):
        self.reply = response(total=0)
        page = self.search()
        self.assertEqual(page.records, [])
        self.assertTrue(page.exhausted)
        self.assertIsNone(page.to_payload()["actual_points"])

    def test_page_500_stops_at_official_display_limit_without_overshooting(self):
        request = build_search_request(500, 2, {"q_organization_name": "lubricant"})
        page = parse_search_response(response(500, 2, 1200), request)
        self.assertTrue(page.exhausted)
        self.assertEqual(page.terminal_reason, "apollo_page_limit")
        self.assertEqual(len(page.records), 2)

    def test_partial_limit_is_distinguished_from_complete_source_exhaustion(self):
        self.reply = response(total=2)
        self.reply["partial_results_only"] = True
        self.assertEqual(self.search().terminal_reason, "apollo_partial_results_limit")

    def test_mixed_response_is_captured_instead_of_dropped_or_counted(self):
        self.reply["accounts"] = [{"id": "synthetic-saved-account", "name": "模拟已保存公司"}]
        capture = self.search()
        self.assertIsInstance(capture, ApolloResponseCapture)
        payload = capture.to_payload()
        self.assertEqual(payload["response_state"], "schema_review_required")
        self.assertEqual(payload["blocking_code"], "mixed_accounts_page_size_mismatch")
        self.assertIsNone(payload["records"])
        self.assertIsNone(payload["exhausted"])
        self.assertEqual(payload["raw"], self.reply)
        self.assertEqual(payload["observed_candidate_count"], 3)
        self.assertEqual(len(payload["review_candidates"]), 3)
        self.assertEqual(payload, capture.to_payload())
        self.assertEqual(len(self.calls), 1)

    def test_mixed_response_within_page_bound_still_does_not_guess_pagination(self):
        self.reply["organizations"] = self.reply["organizations"][:1]
        self.reply["accounts"] = [{"id": "synthetic-account", "organization_id": "synthetic-organization",
                                   "domain": "saved.example", "name": "模拟已保存公司"}]
        payload = self.search().to_payload()
        self.assertEqual(payload["blocking_code"], "mixed_accounts_pagination_unverified")
        self.assertFalse(payload["pagination_verified"])
        self.assertEqual(payload["observed_candidate_count"], 2)
        account = payload["review_candidates"][1]
        self.assertEqual(account["account_id"], "synthetic-account")
        self.assertEqual(account["organization_id"], "synthetic-organization")
        self.assertEqual(account["apollo_id"], "synthetic-organization")
        self.assertEqual(account["domain"], "saved.example")
        self.assertIsNone(payload["actual_points"])

    def test_mixed_incomplete_or_unstructured_records_remain_recoverable(self):
        self.reply["organizations"] = []
        self.reply["accounts"] = [{"id": "account-only", "name": "公司名称"}, {}, "opaque-observation"]
        payload = self.search().to_payload()
        self.assertEqual(payload["raw"], self.reply)
        self.assertEqual(len(payload["review_candidates"]), 3)
        account = payload["review_candidates"][0]
        self.assertEqual(account["account_id"], "account-only")
        self.assertEqual(account["apollo_id"], "")
        self.assertEqual(account["domain"], "")
        self.assertEqual(payload["review_candidates"][1]["raw_record"], {})
        self.assertEqual(payload["review_candidates"][2]["parse_problem"], "row_not_object")
        self.assertIsNone(payload["records"])

    def test_received_bad_pagination_json_is_preserved_for_offline_review(self):
        self.reply["pagination"]["page"] = 99
        payload = self.search().to_payload()
        self.assertEqual(payload["response_state"], "schema_review_required")
        self.assertEqual(payload["blocking_code"], "response_invalid")
        self.assertEqual(payload["raw"]["pagination"]["page"], 99)
        self.assertIsNone(payload["records"])
        self.assertEqual(len(self.calls), 1)

    def test_mixed_raw_response_survives_gateway_restart_and_billing_reconciliation(self):
        from yingba.adapters import UnknownOutcome
        from yingba.budget import Budget, BudgetPause, beijing_day
        from yingba.db import Store
        from yingba.paid import Observation, PaidGateway

        self.reply["accounts"] = [{"id": "account-synthetic", "name": "模拟公司", "echo": FAKE_KEY}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.sqlite"
            store = Store(path)
            Budget(store).confirm(0)
            gateway = PaidGateway(store, lambda action, request: Observation(self.client.search(request, authorized=True).to_payload()), verified=True)
            with self.assertRaises(UnknownOutcome):
                gateway.execute("mixed-response-capture", 1, self.request)
            captured = store.rows("SELECT payload FROM paid_observations WHERE action=?", ("mixed-response-capture",))[0]["payload"]
            self.assertNotIn(FAKE_KEY, captured)
            stored = json.loads(captured)
            self.assertEqual(stored["raw"]["accounts"][0]["echo"], "[REDACTED]")
            self.assertEqual(stored["observed_candidate_count"], 3)
            self.assertEqual(len(self.calls), 1)

            def forbidden_retry(action, request):
                self.fail("already observed paid request must not be repeated")

            resumed = PaidGateway(Store(path), forbidden_retry, verified=True)
            with self.assertRaises(BudgetPause):
                resumed.execute("mixed-response-capture", 1, self.request)
            recovered = resumed.reconcile_captured("mixed-response-capture", 1, beijing_day(), "SIMULATED-BILLING-RECEIPT")
            self.assertEqual(recovered["response_state"], "schema_review_required")
            self.assertIsNone(recovered["records"])
            self.assertIsNone(recovered["exhausted"])
            self.assertEqual(resumed.execute("mixed-response-capture", 1, self.request), recovered)
            self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
