"""Saved account reading: official-shaped data, loopback service, synthetic key."""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import threading
import unittest
from unittest.mock import patch

from yingba.apollo import (ACCOUNTS_PATH, HOST, ApolloAccountsPage, ApolloClient,
                          ApolloPage, ApolloPause, build_accounts_request,
                          parse_accounts_response, request_fingerprint)

FAKE_KEY = "saved-account-fixture-key-not-real"


def accounts_response(page=1, per_page=2, total=3):
    rows = [{"id": f"account-simulation-{i}", "organization_id": f"organization-simulation-{i}",
             "name": f"模拟已保存润滑油公司 {i}", "domain": f"saved{i}.example",
             "modality": "account", "label_ids": ["synthetic-label"], "typed_custom_fields": {"trace": "SIMULATED"}}
            for i in range((page - 1) * per_page, min(page * per_page, total))]
    return {"accounts": rows, "partial_results_only": False,
            "pagination": {"page": page, "per_page": per_page,
                           "total_entries": total, "total_pages": (total + per_page - 1) // per_page}}


class ApolloFreeAccountsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reply = accounts_response()
        self.status = 200
        self.content_type = "application/json"
        self.disconnect = False
        self.extra_headers = {}
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                case.calls.append({"path": self.path, "headers": dict(self.headers),
                                   "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
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
                self.wfile.write(body)

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
        self.request = build_accounts_request(1, 2)

    def tearDown(self):
        self.patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def read(self):
        return self.client.search_accounts(self.request)

    def test_official_json_body_path_header_and_no_paid_or_mutating_calls(self):
        self.request = build_accounts_request(1, 2, {
            "q_organization_name": "润滑油", "account_label_ids": ["label-b", "label-a"],
            "account_stage_ids": ["stage-a"], "sort_by_field": "account_created_at", "sort_ascending": True,
        })
        page = self.read()
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["path"], ACCOUNTS_PATH)
        self.assertNotIn("?", call["path"])
        self.assertEqual(json.loads(call["body"]), self.request)
        self.assertEqual(call["headers"]["x-api-key"], FAKE_KEY)
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertNotIn(FAKE_KEY, call["body"].decode())
        self.assertIsInstance(page, ApolloAccountsPage)
        self.assertIsInstance(page, ApolloPage)
        self.assertTrue(all(c["path"] == ACCOUNTS_PATH for c in self.calls))

    def test_free_provenance_is_explicit_and_not_a_paid_receipt(self):
        payload = self.read().to_payload()
        self.assertEqual(payload["provenance"], "saved_accounts")
        self.assertEqual(payload["billing_status"], "documented_free")
        self.assertEqual(payload["actual_points"], 0)
        self.assertEqual(payload["documented_estimate_points"], 0)
        self.assertEqual(payload["source_url"], "https://api.apollo.io" + ACCOUNTS_PATH)
        self.assertNotIn("external_reference", payload)
        self.assertEqual(payload["raw"], self.reply)
        self.assertEqual(payload["request_fingerprint"], request_fingerprint(self.request))

    def test_account_id_is_never_used_as_organization_id(self):
        self.reply["accounts"][1]["organization_id"] = None
        rows = self.read().records
        self.assertEqual(rows[0]["account_id"], "account-simulation-0")
        self.assertEqual(rows[0]["organization_id"], "organization-simulation-0")
        self.assertEqual(rows[0]["apollo_id"], "organization-simulation-0")
        self.assertEqual(rows[1]["account_id"], "account-simulation-1")
        self.assertEqual(rows[1]["organization_id"], "")
        self.assertEqual(rows[1]["apollo_id"], "")
        self.assertEqual(rows[1]["apollo_raw"]["organization_id"], None)

    def test_incomplete_company_rows_and_raw_objects_are_preserved(self):
        self.reply["accounts"] = [{"id": "name-only", "name": "未确认官网公司", "organization_id": None}, {}]
        rows = self.read().records
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["domain"], "")
        self.assertEqual(rows[0]["apollo_id"], "")
        self.assertEqual(rows[0]["apollo_raw"], self.reply["accounts"][0])
        self.assertEqual(rows[1]["apollo_raw"], {})
        self.assertEqual(rows[1]["name"], "")
        for row in rows:
            self.assertNotIn("qualified", row)
            self.assertNotIn("emails", row)
            self.assertEqual(row["provenance"], "saved_accounts")

    def test_website_is_not_guessed_from_account_name(self):
        self.reply["accounts"][0].pop("domain")
        self.reply["accounts"][0]["name"] = "Example Lubricant International"
        row = self.read().records[0]
        self.assertEqual(row["domain"], "")
        self.assertEqual(row["website_url"], "")

    def test_two_pages_and_surplus_are_preserved_until_saved_accounts_exhausted(self):
        first = self.read()
        self.request = build_accounts_request(2, 2)
        self.reply = accounts_response(2, 2, 3)
        second = self.read()
        self.assertFalse(first.exhausted)
        self.assertTrue(second.exhausted)
        self.assertEqual(second.terminal_reason, "saved_accounts_exhausted")
        self.assertEqual(len(first.records) + len(second.records), 3)
        self.assertEqual(json.loads(self.calls[1]["body"])["page"], 2)
        self.assertTrue(all(c["path"] == ACCOUNTS_PATH for c in self.calls))

    def test_empty_saved_account_list_is_empty_without_paid_fallback(self):
        self.reply = accounts_response(total=0)
        result = self.read()
        self.assertEqual(result.records, [])
        self.assertTrue(result.exhausted)
        self.assertEqual(result.terminal_reason, "saved_accounts_exhausted")
        self.assertEqual([c["path"] for c in self.calls], [ACCOUNTS_PATH])

    def test_500_page_cap_is_explicit_and_partial_response_pauses(self):
        request = build_accounts_request(500, 2)
        raw = accounts_response(500, 2, 1200)
        page = parse_accounts_response(raw, request)
        self.assertTrue(page.exhausted)
        self.assertEqual(page.terminal_reason, "apollo_page_limit")
        raw["partial_results_only"] = True
        with self.assertRaises(ApolloPause):
            parse_accounts_response(raw, request)

    def test_explicit_totals_cannot_silently_truncate_or_extend_page_count(self):
        for total, pages in ((1000, 1), (3, 1), (3, 3), (1, 1)):
            with self.subTest(total=total, pages=pages):
                raw = accounts_response()
                raw['pagination'].update(total_entries=total, total_pages=pages)
                with self.assertRaises(ApolloPause):
                    parse_accounts_response(raw, self.request)

    def test_short_middle_or_final_page_is_not_verified_as_complete(self):
        for page, total in ((1, 4), (2, 4), (2, 3)):
            with self.subTest(page=page, total=total):
                raw = accounts_response(page, 2, total)
                raw['accounts'].pop()
                with self.assertRaises(ApolloPause):
                    parse_accounts_response(raw, build_accounts_request(page, 2))
        raw = accounts_response(2, 2, 3)
        raw['accounts'].append({'id': 'unexpected-extra'})
        with self.assertRaises(ApolloPause):
            parse_accounts_response(raw, build_accounts_request(2, 2))

    def test_partial_flags_always_pause_and_empty_page_must_be_first(self):
        for page in (1, 2):
            for flag in (True, 1, 'false', None):
                with self.subTest(page=page, flag=flag):
                    raw = accounts_response(page, 2, 3)
                    raw['partial_results_only'] = flag
                    with self.assertRaises(ApolloPause):
                        parse_accounts_response(raw, build_accounts_request(page, 2))
        for pages in (0, 1):
            raw = accounts_response(total=0)
            raw['pagination']['total_pages'] = pages
            self.assertTrue(parse_accounts_response(raw, self.request).exhausted)
        raw = accounts_response(2, 2, 0)
        with self.assertRaises(ApolloPause):
            parse_accounts_response(raw, build_accounts_request(2, 2))

    def test_inconsistent_pagination_from_http_pauses_without_retry(self):
        self.reply['pagination'].update(total_entries=1000, total_pages=1)
        with self.assertRaises(ApolloPause) as caught:
            self.read()
        self.assertEqual(caught.exception.code, 'accounts_response_invalid')
        self.assertFalse(caught.exception.billing_unknown)
        self.assertEqual(len(self.calls), 1)

    def test_401_403_429_pause_without_master_key_upgrade_retry_or_enrichment(self):
        for status, code in ((401, "authentication"), (403, "permission"), (429, "rate_limit")):
            with self.subTest(status=status):
                self.status = status
                self.reply = {"error": "simulated rejected key: " + FAKE_KEY}
                before = len(self.calls)
                with self.assertRaises(ApolloPause) as caught:
                    self.read()
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.billing_unknown)
                self.assertNotIn(FAKE_KEY, str(caught.exception))
                self.assertEqual(len(self.calls), before + 1)
        self.assertTrue(all(c["path"] == ACCOUNTS_PATH for c in self.calls))

    def test_disconnection_pauses_read_without_paid_uncertainty_or_retry(self):
        self.disconnect = True
        with self.assertRaises(ApolloPause) as caught:
            self.read()
        self.assertFalse(caught.exception.billing_unknown)
        self.assertEqual(len(self.calls), 1)

    def test_redirect_never_forwards_key(self):
        self.status = 307
        self.extra_headers["Location"] = "https://evil.example"
        with self.assertRaises(ApolloPause) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "redirect_refused")
        self.assertEqual(len(self.calls), 1)

    def test_incomplete_or_inconsistent_pagination_pauses(self):
        invalid = []
        for key, value in (("page", 2), ("per_page", 100), ("total_entries", -1), ("total_pages", 0), ("page", True)):
            raw = accounts_response()
            raw["pagination"][key] = value
            invalid.append(raw)
        raw = accounts_response(); raw["accounts"] = []; invalid.append(raw)
        raw = accounts_response(); raw["accounts"].append({}); invalid.append(raw)
        raw = accounts_response(); raw["accounts"] = [None]; invalid.append(raw)
        raw = accounts_response(); raw["organizations"] = [{"id": "unexpected"}]; invalid.append(raw)
        raw = accounts_response(); raw["pagination"].pop("total_pages"); invalid.append(raw)
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ApolloPause) as caught:
                    parse_accounts_response(raw, self.request)
                self.assertFalse(caught.exception.billing_unknown)

    def test_request_validation_permits_only_documented_body_fields(self):
        self.assertEqual(build_accounts_request(1, 100), {"page": 1, "per_page": 100})
        invalid = [{"q_organization_keyword_tags": ["lubricant"]}, {"api_key": FAKE_KEY},
                   {"organization_locations": ["Japan"]}, {"reveal_personal_emails": True},
                   {"account_label_ids": "label"}, {"sort_ascending": True},
                   {"sort_by_field": "domain"}, {"sort_by_field": "account_created_at", "sort_ascending": 1},
                   {"q_organization_name": ""}, {"q_organization_name": "x\ny"}]
        for filters in invalid:
            with self.subTest(filters=filters):
                with self.assertRaises(ValueError):
                    build_accounts_request(1, 2, filters)
        self.assertFalse(self.connection.called)

    def test_boundaries_are_validated_before_network(self):
        for page, per_page in ((0, 2), (501, 2), (True, 2), (1, 101), (1, 0), (1, False)):
            with self.assertRaises(ValueError):
                build_accounts_request(page, per_page)
        bad = build_accounts_request(1, 2, {"account_label_ids": ["a", "b"]})
        bad["account_label_ids"] = ["b", "a"]
        with self.assertRaises(ValueError):
            self.client.search_accounts(bad)
        self.assertFalse(self.connection.called)

    def test_array_filters_are_canonical_and_search_body_immutable(self):
        filters = {"account_label_ids": ["z", "a", "z"]}
        original = copy.deepcopy(filters)
        a = build_accounts_request(1, 2, filters)
        b = build_accounts_request(1, 2, {"account_label_ids": ["a", "z"]})
        self.assertEqual(a, b)
        self.assertEqual(filters, original)
        self.assertEqual(request_fingerprint(a), request_fingerprint(b))

    def test_disabled_or_missing_credentials_do_not_make_free_call(self):
        for client in (ApolloClient(FAKE_KEY), ApolloClient(None, enabled=True)):
            with self.assertRaises(ApolloPause):
                client.search_accounts(self.request)
        self.assertFalse(self.connection.called)

    def test_echoed_credential_never_enters_captured_raw_data(self):
        self.reply["accounts"][0]["typed_custom_fields"]["echo"] = FAKE_KEY
        self.reply["accounts"][0]["api_key"] = FAKE_KEY
        self.assertNotIn(FAKE_KEY, json.dumps(self.read().to_payload()))

    def test_bad_json_or_wrong_response_content_never_becomes_empty_success(self):
        for body, content_type in ((b"not-json", "application/json"), (b"{}", "text/html"),
                                   (b'{"a": 1, "a": 2}', "application/json"), (b"[]", "application/json")):
            self.reply, self.content_type = body, content_type
            before = len(self.calls)
            with self.assertRaises(ApolloPause):
                self.read()
            self.assertEqual(len(self.calls), before + 1)


if __name__ == "__main__":
    unittest.main()
