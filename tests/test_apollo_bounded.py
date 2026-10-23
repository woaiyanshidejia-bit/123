"""Strict response consistency checks for bounded paid authorization."""
import copy
import json
import unittest
from unittest.mock import patch

from yingba.apollo import (ApolloAccountsPage, ApolloPage, build_search_request,
                          parse_search_response, request_fingerprint,
                          validate_bounded_search_payload)
from tests.test_apollo_transport import response


class BoundedSearchPayloadTests(unittest.TestCase):
    def setUp(self):
        self.request = build_search_request(1, 2, {"q_organization_name": "lubricant"})
        self.payload = parse_search_response(response(), self.request).to_payload()

    def valid(self, payload=None, request=None):
        return validate_bounded_search_payload(self.payload if payload is None else payload,
                                               self.request if request is None else request)

    def test_correct_response_and_json_roundtrip_pass_without_network(self):
        with patch("yingba.apollo.http.client.HTTPSConnection") as network:
            self.assertTrue(self.valid())
            self.assertTrue(self.valid(json.loads(json.dumps(self.payload))))
            network.assert_not_called()

    def test_empty_valid_response_is_complete_but_not_fabricated_companies(self):
        payload = parse_search_response(response(total=0), self.request).to_payload()
        self.assertTrue(self.valid(payload))
        self.assertEqual(payload["records"], [])

    def test_mismatched_or_noncanonical_original_query_is_rejected(self):
        changed = build_search_request(1, 2, {"q_organization_name": "different company"})
        self.assertFalse(self.valid(request=changed))
        for key, value in (("page", 2), ("per_page", True), ("api_key", "synthetic-key")):
            request = dict(self.request)
            request[key] = value
            self.assertFalse(self.valid(request=request))

    def test_tampered_records_cannot_be_accepted_as_complete_observation(self):
        for key, value in (("name", "wrong company"), ("domain", "wrong.example"),
                           ("apollo_id", "wrong-id"), ("source_url", "https://evil.example"),
                           ("collected", "2026-10-01T00:00:00+00:00")):
            payload = copy.deepcopy(self.payload)
            payload["records"][0][key] = value
            self.assertFalse(self.valid(payload), key)
        for records in ([], self.payload["records"][:1], list(reversed(self.payload["records"]))):
            payload = copy.deepcopy(self.payload)
            payload["records"] = records
            self.assertFalse(self.valid(payload))

    def test_tampered_fingerprint_source_billing_and_terminal_flags_fail(self):
        for key, value in (("request_fingerprint", "0" * 64),
                           ("source_url", "https://api.apollo.io/api/v1/accounts/search"),
                           ("billing_status", "documented_free"), ("actual_points", 0),
                           ("documented_estimate_points", True), ("exhausted", True),
                           ("exhausted", 0), ("terminal_reason", "search_exhausted"),
                           ("billing_note", "already paid")):
            payload = copy.deepcopy(self.payload)
            payload[key] = value
            self.assertFalse(self.valid(payload), key)

    def test_raw_pagination_and_derived_pagination_must_both_be_consistent(self):
        payload = copy.deepcopy(self.payload)
        payload["pagination"]["total_entries"] += 1
        self.assertFalse(self.valid(payload))
        payload = copy.deepcopy(self.payload)
        payload["raw"]["pagination"]["page"] = 99
        self.assertFalse(self.valid(payload))
        payload = copy.deepcopy(self.payload)
        payload["raw"]["organizations"][0]["name"] = "changed raw"
        self.assertFalse(self.valid(payload))

    def test_partial_or_internally_incomplete_page_is_not_automatic_success(self):
        cases = []
        raw = response(); raw["partial_results_only"] = True; cases.append(raw)
        raw = response(); raw["partial_results_only"] = "false"; cases.append(raw)
        raw = response(); raw["organizations"] = raw["organizations"][:1]; cases.append(raw)
        raw = response(); raw["pagination"]["total_pages"] = 1; cases.append(raw)
        for raw in cases:
            payload = parse_search_response(raw, self.request).to_payload()
            self.assertFalse(self.valid(payload))

    def test_capture_is_rejected_even_if_list_and_bool_flags_are_forged(self):
        raw = response()
        raw["accounts"] = [{"id": "account-id", "name": "Saved Company"}]
        capture = parse_search_response(raw, self.request).to_payload()
        self.assertFalse(self.valid(capture))
        capture["records"] = self.payload["records"]
        capture["exhausted"] = False
        self.assertFalse(self.valid(capture))
        forged = copy.deepcopy(self.payload)
        forged["raw"] = raw
        self.assertFalse(self.valid(forged))

    def test_only_exact_apollo_page_type_is_accepted(self):
        original = parse_search_response(response(), self.request)
        for cls in (ApolloAccountsPage, type("OtherPage", (ApolloPage,), {})):
            substitute = cls(**original.__dict__)
            with patch("yingba.apollo.parse_search_response", return_value=substitute):
                self.assertFalse(self.valid())

    def test_missing_extra_and_nonfinite_fields_fail_closed(self):
        for key in ("actual_points", "provider_request_id", "raw", "pagination", "billing_note"):
            payload = copy.deepcopy(self.payload)
            payload.pop(key)
            self.assertFalse(self.valid(payload), key)
        for key, value in (("response_state", "success"), ("verified", True), ("n", float("nan"))):
            payload = copy.deepcopy(self.payload)
            payload[key] = value
            self.assertFalse(self.valid(payload))

    def test_invalid_timestamps_and_request_identifiers_do_not_pass(self):
        for timestamp in (None, "not-a-time", "2026-10-23T12:00:00", "2026-10-23T12:00:00+08:00"):
            payload = copy.deepcopy(self.payload)
            payload["collected"] = timestamp
            self.assertFalse(self.valid(payload))
        for provider_id in (False, 42, "injected\nvalue", " ", "x" * 201):
            payload = copy.deepcopy(self.payload)
            payload["provider_request_id"] = provider_id
            self.assertFalse(self.valid(payload))

    def test_unparseable_input_returns_false_instead_of_raising(self):
        for payload in (None, [], "data", 1, {"records": [], "exhausted": True}):
            self.assertIs(validate_bounded_search_payload(payload, self.request), False)
        for request in (None, [], "request", {}, {"page": 1, "per_page": 2}):
            self.assertIs(validate_bounded_search_payload(self.payload, request), False)
        payload = copy.deepcopy(self.payload)
        payload["raw"]["cyclic"] = payload
        self.assertIs(self.valid(payload), False)


if __name__ == "__main__":
    unittest.main()
