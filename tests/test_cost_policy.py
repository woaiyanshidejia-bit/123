"""Cost gates exercise local fixtures only; no Apollo, Codex, or user computer."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from yingba.adapters import ConnectionPause
from yingba.apollo import ACCOUNTS_PATH, SEARCH_PATH
from yingba.budget import Budget, BudgetPause, beijing_day
from yingba.cost_policy import (ACCOUNT_DOC, ORGANIZATION_DOC, PRICING_DOC,
                                CostPolicy, OfficialDocumentReader)
from yingba.db import Store
from yingba.paid import PaidGateway


FREE = f'''# Search for Accounts
POST https://api.apollo.io{ACCOUNTS_PATH}
| **Credit usage** | 0 credits |
Search previously saved accounts. Authentication and permissions are required.
'''
PAID = f'''# Organization Search
POST https://api.apollo.io{SEARCH_PATH}
| **Credit usage** | 1 credit per page |
This endpoint consumes 1 Apollo credit per page, with up to 100 results per page.
'''
PRICING = '''# API Pricing
Currently available plans use this table. Legacy plans may have other charges.
| Organization search | 1 credit per page, up to 100 results per page. |
Check your account's About credits page for the current applicable plan.
'''


def account(**changes):
    result = {'source_url': 'https://app.apollo.io/#/settings/plans',
              'plan': 'Professional', 'text': 'Professional\nOrganization Search\n1 credit per page',
              'confirmed_current_tariff': True, 'upper_bound': 1, 'no_external_spend': True}
    result.update(changes)
    return result


class Documents:
    def __init__(self):
        self.docs = {ACCOUNT_DOC: FREE, ORGANIZATION_DOC: PAID, PRICING_DOC: PRICING}
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return {'url': url, 'text': self.docs[url], 'observed_at': datetime.now(timezone.utc).isoformat()}


class Reviewer:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def review_cost(self, action, evidence, *, enabled=False):
        self.calls.append((action, evidence, enabled))
        paid = action['endpoint'] == SEARCH_PATH
        refs = [{'evidence_id': item['id'], 'quote': item['text']} for item in evidence]
        result = {'classification': 'paid' if paid else 'free', 'endpoint': action['endpoint'],
                  'unit': 'page', 'max_points': 1 if paid else 0,
                  'applicable_plan': action['applicable_plan'], 'effective_at': None,
                  'valid_until': None, 'refs': refs, 'reason': 'Local fixture evidence agrees.'}
        if self.mutate:
            self.mutate(result, action, evidence)
        return result


class CostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'fees.sqlite')
        self.docs = Documents()
        self.reviewer = Reviewer()
        self.costs = CostPolicy(self.store, self.reviewer, self.docs)

    def tearDown(self):
        self.temp.cleanup()

    def free(self, **overrides):
        values = dict(source='apollo_accounts', account_label='本人账号', filters={}, per_page=100)
        values.update(overrides)
        return self.costs.assess(**values)

    def paid(self, **overrides):
        values = dict(source='apollo_organizations', account_label='本人账号',
                      filters={'q_organization_keyword_tags': ['lubricants']}, per_page=100,
                      account_evidence=account())
        values.update(overrides)
        return self.costs.assess(**values)

    def test_free_needs_no_budget_and_persists_evidence(self):
        result = self.free(authorized=True)
        self.assertEqual((result['classification'], result['max_points'], result['policy_id']), ('free', 0, None))
        self.assertEqual(self.store.rows('SELECT * FROM budgets'), [])
        self.assertEqual(self.store.rows('SELECT * FROM charges'), [])
        self.assertEqual(self.store.rows('SELECT * FROM paid_policies'), [])
        stored = self.store.rows('SELECT * FROM cost_assessments')[0]
        self.assertEqual(stored['classification'], 'free')
        self.assertEqual(json.loads(stored['evidence'])[0]['url'], ACCOUNT_DOC)
        self.assertTrue(self.reviewer.calls[0][2])
        self.assertEqual(self.reviewer.calls[0][0]['page_size'], 100)

    def test_free_not_blocked_by_old_paid_unknown(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO charges VALUES('uncertain','2020-01-01',1,NULL,'unknown')")
        self.assertEqual(self.free()['max_points'], 0)
        self.assertEqual(self.store.rows('SELECT status FROM charges')[0]['status'], 'unknown')

    def test_free_same_day_cache_survives_new_service(self):
        first = self.free()
        self.costs = CostPolicy(Store(self.store.path), self.reviewer, self.docs)
        second = self.free()
        self.assertEqual(first['assessment_id'], second['assessment_id'])
        self.assertTrue(second['cached'])
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertEqual(len(self.docs.calls), 1)

    def test_concurrent_same_service_evaluates_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.free(), range(4)))
        self.assertEqual(len({r['assessment_id'] for r in results}), 1)
        self.assertEqual(len(self.reviewer.calls), 1)

    def test_cache_binds_account_filters_and_page_size(self):
        self.free()
        self.free(account_label='另一个账号')
        self.free(filters={'q_organization_name': 'oil'})
        self.free(per_page=50)
        self.assertEqual(len(self.reviewer.calls), 4)

    def test_cache_never_survives_beijing_day(self):
        self.free()
        with patch('yingba.cost_policy.beijing_day', return_value='2099-01-02'):
            self.free()
        self.assertEqual(len(self.reviewer.calls), 2)

    def test_unknown_model_verdict_pauses_and_is_recorded_as_unknown(self):
        self.reviewer.mutate = lambda result, *_: result.update(classification='unknown', max_points=None)
        with self.assertRaises(ConnectionPause):
            self.free()
        row = self.store.rows('SELECT * FROM cost_assessments')[0]
        self.assertEqual((row['classification'], row['max_points'], row['policy_id']), ('unknown', None, None))

    def test_official_zero_must_be_explicit_before_model(self):
        for content in (FREE.replace('0 credits', 'unknown'), FREE.replace('0 credits', '1 credit'), FREE.replace(ACCOUNTS_PATH, SEARCH_PATH)):
            with self.subTest(content=content):
                self.docs.docs[ACCOUNT_DOC] = content
                with self.assertRaises(ConnectionPause):
                    self.free()
        self.assertEqual(self.reviewer.calls, [])

    def test_paid_cannot_be_reclassified_free_by_model(self):
        self.reviewer.mutate = lambda result, *_: result.update(classification='free', max_points=0)
        with self.assertRaises(ConnectionPause):
            self.paid(authorized=True)
        self.assertEqual(self.store.rows('SELECT * FROM paid_policies'), [])

    def test_review_unit_plan_integer_and_refs_are_bound(self):
        changes = ({'unit': 'record'}, {'endpoint': ACCOUNTS_PATH}, {'max_points': True},
                   {'max_points': 1.0}, {'max_points': 11}, {'applicable_plan': 'Other'},
                   {'refs': [{'evidence_id': 'other', 'quote': '1 credit'}]},
                   {'refs': [{'evidence_id': 'endpoint', 'quote': 'not in document'}]})
        for change in changes:
            with self.subTest(change=change):
                self.reviewer.mutate = lambda result, *_, change=change: result.update(change)
                with self.assertRaises(ConnectionPause):
                    self.paid()

    def test_paid_quotes_require_official_rate_and_account_plan(self):
        def omit_rate(result, *_):
            result['refs'][0]['quote'] = '# Organization Search'
        self.reviewer.mutate = omit_rate
        with self.assertRaises(ConnectionPause):
            self.paid()
        def omit_plan(result, *_):
            result['refs'][-1]['quote'] = '1 credit per page'
        self.reviewer.mutate = omit_plan
        with self.assertRaises(ConnectionPause):
            self.paid()

    def test_missing_and_legacy_account_cannot_reach_model(self):
        cases = [None, account(plan='Legacy Professional'), account(text='Professional legacy plan 1 credit per page'),
                 account(confirmed_current_tariff=False), account(no_external_spend=False),
                 account(upper_bound=True), account(upper_bound=0), account(plan='Other'),
                 account(text='Professional Organization search: pricing may vary'),
                 account(text='Professional Organization Search 11 credits per page'),
                 account(text='Professional Organization Search 1.1 credits per page'),
                 account(text='Professional Organization Search 1 credit per page; otherwise 2 credits per page')]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ConnectionPause):
                    self.paid(account_evidence=value)
        self.assertEqual(self.reviewer.calls, [])
        self.assertEqual(self.store.rows('SELECT * FROM cost_assessments'), [])

    def test_account_credentials_never_saved_or_sent(self):
        samples = [account(text=account()['text'] + '\n' + key + ' = SensitiveSecret123')
                   for key in ('api_key', 'token', 'refresh_token', 'password', 'authorization', 'credential')]
        samples += [account(api_key='SensitiveSecret123'), account(plan='password: SensitiveSecret123'),
                    account(source_url='https://app.apollo.io/?api_key=SensitiveSecret123'),
                    account(source_url='https://app.apollo.io/#/plans?token=SensitiveSecret123'),
                    account(source_url='https://app.apollo.io/#password/SensitiveSecret123')]
        for value in samples:
            with self.subTest(value=value):
                with self.assertRaises(ConnectionPause) as caught:
                    self.paid(account_evidence=value)
                self.assertNotIn('SensitiveSecret123', str(caught.exception))
        self.assertEqual(self.reviewer.calls, [])
        self.assertEqual(self.store.rows('SELECT * FROM cost_assessments'), [])

    def test_account_source_host_and_username_and_label_are_strict(self):
        for url in ('http://app.apollo.io/', 'https://app.apollo.io.attacker.invalid/',
                    'https://docs.apollo.io/', 'https://user:password@app.apollo.io/', 'https://app.apollo.io:8443/'):
            with self.subTest(url=url), self.assertRaises(ConnectionPause):
                self.paid(account_evidence=account(source_url=url))
        with self.assertRaises(ConnectionPause):
            self.paid(account_evidence=account(account_label='another'))

    def test_paid_assessment_does_not_implicitly_authorize(self):
        result = self.paid()
        self.assertEqual((result['classification'], result['max_points'], result['policy_id']), ('paid', 1, None))
        self.assertIn('未在线认证', result['account_provenance'])
        self.assertEqual(self.docs.calls, [ORGANIZATION_DOC, PRICING_DOC])
        self.assertEqual(self.store.rows('SELECT * FROM charges'), [])
        self.assertEqual(self.store.rows('SELECT * FROM paid_policies'), [])

    def test_paid_authorization_needs_baseline(self):
        with self.assertRaisesRegex(ConnectionPause, '今日用量'):
            self.paid(authorized=True)
        self.assertEqual(self.store.rows('SELECT * FROM paid_policies'), [])
        self.assertEqual(self.store.rows('SELECT classification FROM cost_assessments')[0]['classification'], 'paid')

    def test_paid_authorization_creates_bound_policy_without_spending(self):
        Budget(self.store).confirm(0)
        result = self.paid(authorized=True)
        policy = self.store.rows('SELECT * FROM paid_policies')[0]
        self.assertEqual(policy['id'], result['policy_id'])
        self.assertEqual(policy['upper_bound'], 1)
        request = json.loads(policy['request_template'])
        self.assertEqual(request['endpoint'], SEARCH_PATH)
        self.assertEqual(request['query']['page'], 1)
        self.assertEqual(request['query']['per_page'], 100)
        self.assertEqual(request['account_label'], '本人账号')
        self.assertEqual(self.store.rows('SELECT * FROM charges'), [])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        evidence = json.loads(policy['evidence'])
        self.assertTrue(evidence['authorized'])
        self.assertIn('用户声明', evidence['documents'][-1]['attestation'])
        with self.store.transaction() as db:
            PaidGateway(self.store)._policy(db, policy['id'], request, 1, current=True)
            request['query']['per_page'] = 20
            with self.assertRaises(BudgetPause):
                PaidGateway(self.store)._policy(db, policy['id'], request, 1, current=True)

    def test_paid_exhausted_budget_and_all_unresolved_states_pause(self):
        Budget(self.store).confirm(50)
        with self.assertRaisesRegex(ConnectionPause, '50分'):
            self.paid(authorized=True)
        with self.store.transaction() as db:
            db.execute('UPDATE budgets SET used=0')
        for state in ('reserved', 'unknown', 'observed_pending'):
            with self.subTest(state=state):
                with self.store.transaction() as db:
                    db.execute('DELETE FROM charges')
                    db.execute('INSERT INTO charges VALUES(?,?,1,NULL,?)', ('pending', beijing_day(), state))
                with self.assertRaises(ConnectionPause):
                    self.paid(authorized=True)
        self.assertEqual(self.store.rows('SELECT * FROM paid_policies'), [])

    def test_account_text_change_invalidates_assessment_cache(self):
        self.paid()
        self.paid(account_evidence=account(text=account()['text'] + '\nCurrent account tariff confirmed.'))
        self.assertEqual(len(self.reviewer.calls), 2)

    def test_cached_paid_assessment_never_returns_previous_authority(self):
        Budget(self.store).confirm(0)
        initial = self.paid(authorized=True)
        self.assertIsNotNone(initial['policy_id'])
        self.assertIsNone(self.paid(authorized=False)['policy_id'])
        PaidGateway(self.store).revoke_bounded(initial['policy_id'])
        repeated = self.paid(authorized=False)
        self.assertTrue(repeated['cached'])
        self.assertIsNone(repeated['policy_id'])
        self.assertEqual(len(self.reviewer.calls), 1)

    def test_pricing_and_endpoint_must_agree_before_model(self):
        self.docs.docs[PRICING_DOC] = PRICING.replace('1 credit', '11 credits')
        with self.assertRaises(ConnectionPause):
            self.paid()
        self.docs.docs[PRICING_DOC] = PRICING.replace('Legacy plans', 'Unspecified')
        with self.assertRaises(ConnectionPause):
            self.paid()
        self.docs.docs[PRICING_DOC] = PRICING
        self.docs.docs[ORGANIZATION_DOC] = PAID.replace('1 Apollo credit', '2 Apollo credits')
        with self.assertRaises(ConnectionPause):
            self.paid()
        self.assertEqual(self.reviewer.calls, [])

    def test_null_effective_dates_allowed_without_inventing_dates(self):
        result = self.paid()
        review = json.loads(self.store.rows('SELECT model_review FROM cost_assessments')[0]['model_review'])
        self.assertIsNone(review['effective_at'])
        self.assertIsNone(review['valid_until'])
        self.assertEqual(result['classification'], 'paid')

    def test_unquoted_future_expired_or_naive_dates_pause(self):
        for field, value in (('effective_at', '2099-01-01'), ('valid_until', '2000-01-01'),
                             ('effective_at', '2000-01-01T00:00:00')):
            with self.subTest(value=value):
                self.docs.docs[ACCOUNT_DOC] = FREE + '\n' + value
                self.reviewer.mutate = lambda result, *_, field=field, value=value: result.update({field: value})
                with self.assertRaises(ConnectionPause):
                    self.free()
        self.docs.docs[ACCOUNT_DOC] = FREE
        self.reviewer.mutate = lambda result, *_: result.update(effective_at='2000-01-01')
        with self.assertRaises(ConnectionPause):
            self.free()

    def test_cross_day_during_review_pauses_without_issuing_policy(self):
        clock = ['2026-10-23']
        self.reviewer.mutate = lambda *_: clock.__setitem__(0, '2026-10-24')
        with patch('yingba.cost_policy.beijing_day', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(ConnectionPause, '跨越'):
                self.free()
        self.assertEqual(self.store.rows('SELECT classification FROM cost_assessments')[0]['classification'], 'unknown')

    def test_invalid_source_inputs_do_not_fetch(self):
        for values in ({'source': 'apollo_import'}, {'source': 'unknown'}, {'per_page': True},
                       {'per_page': 101}, {'filters': {'api_key': 'do-not-save'}}, {'account_label': 'token=do-not-save'}):
            with self.subTest(values=values), self.assertRaises(ConnectionPause):
                self.free(**values)
        self.assertEqual(self.docs.calls, [])


class DocumentationReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = {}
        cls.calls = []
        outer = cls
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                outer.calls.append((self.path, dict(self.headers)))
                status, headers, body, delay = outer.routes[self.path]
                if delay:
                    try:
                        for char in b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n':
                            self.wfile.write(bytes([char])); self.wfile.flush(); time.sleep(delay)
                    except (OSError, ValueError):
                        pass
                    return
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)

    def setUp(self):
        self.calls.clear()
        self.routes.clear()
        self.routes['/reference/search-for-accounts.md'] = (200, {'Content-Type': 'text/markdown'}, FREE.encode(), 0)
        self.hosts = []

    def connection(self, host, *, timeout, context):
        self.hosts.append(host)
        self.assertTrue(context.check_hostname)
        return http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=timeout)

    def read(self, **kwargs):
        return OfficialDocumentReader(connection_factory=self.connection, **kwargs).fetch(ACCOUNT_DOC)

    def test_fixed_https_target_only_no_credentials_or_cookie(self):
        result = self.read()
        self.assertEqual(result['url'], ACCOUNT_DOC + '.md')
        self.assertIn('0 credits', result['text'])
        self.assertEqual(self.hosts, ['docs.apollo.io'])
        self.assertEqual(len(result['content_sha256']), 64)
        self.assertIsNotNone(datetime.fromisoformat(result['observed_at']).tzinfo)
        headers = self.calls[0][1]
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('X-Api-Key', headers)
        for url in ('https://api.apollo.io/api/v1/accounts/search', 'http://docs.apollo.io/reference/search-for-accounts',
                    ACCOUNT_DOC + '?token=sensitive', 'https://localhost/'):
            with self.subTest(url=url), self.assertRaises(ConnectionPause):
                OfficialDocumentReader(self.connection).fetch(url)
        self.assertEqual(len(self.calls), 1)

    def test_markdown_404_html_fallback_normalizes_ignores_scripts(self):
        self.routes['/reference/search-for-accounts.md'] = (404, {}, b'', 0)
        body = ('<html><script>apikey=do-not-render</script><style>hidden text</style><h1>Accounts</h1>'
                '<p>POST https://api.apollo.io' + ACCOUNTS_PATH + '</p><table><tr><td>Credit usage</td><td>0 credits</td></tr></table></html>').encode()
        self.routes['/reference/search-for-accounts'] = (200, {'Content-Type': 'text/html; charset=utf-8'}, body, 0)
        result = self.read()
        self.assertEqual(result['url'], ACCOUNT_DOC)
        self.assertNotIn('do-not-render', result['text'])
        self.assertNotIn('hidden text', result['text'])
        self.assertIn('0 credits', result['text'])

    def test_redirects_refuse_foreign_api_and_other_official_page(self):
        for target in ('https://attacker.invalid/', 'https://api.apollo.io/api/v1/mixed_companies/search', ORGANIZATION_DOC):
            with self.subTest(target=target):
                self.routes['/reference/search-for-accounts.md'] = (302, {'Location': target}, b'', 0)
                before = len(self.calls)
                with self.assertRaises(ConnectionPause):
                    self.read()
                self.assertEqual(len(self.calls), before + 1)

    def test_redirect_loop_is_bounded(self):
        self.routes['/reference/search-for-accounts.md'] = (302, {'Location': ACCOUNT_DOC + '.md'}, b'', 0)
        with self.assertRaises(ConnectionPause):
            self.read()
        self.assertEqual(len(self.calls), 3)

    def test_wrong_status_compression_format_and_encoding_rejected(self):
        cases = [(403, {'Content-Type': 'text/plain'}, b'Forbidden', 0),
                 (200, {'Content-Type': 'application/json'}, b'{}', 0),
                 (200, {'Content-Type': 'text/plain', 'Content-Encoding': 'gzip'}, b'compressed', 0),
                 (200, {'Content-Type': 'text/plain'}, b'\xff\xfe', 0)]
        for route in cases:
            with self.subTest(route=route):
                self.routes['/reference/search-for-accounts.md'] = route
                with self.assertRaises(ConnectionPause):
                    self.read()

    def test_body_and_normalized_text_size_limits(self):
        with patch.object(OfficialDocumentReader, 'MAX_BYTES', 20):
            with self.assertRaises(ConnectionPause):
                self.read()
        with patch.object(OfficialDocumentReader, 'MAX_TEXT', 20):
            with self.assertRaises(ConnectionPause):
                self.read()

    def test_slow_headers_cannot_extend_total_deadline(self):
        self.routes['/reference/search-for-accounts.md'] = (200, {}, b'', .03)
        start = time.monotonic()
        with self.assertRaises(ConnectionPause):
            self.read(timeout=.25)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_dns_has_total_deadline(self):
        event = threading.Event()
        def resolver(*args, **kwargs):
            event.wait(2)
            return []
        start = time.monotonic()
        try:
            with self.assertRaises(ConnectionPause):
                OfficialDocumentReader(timeout=.1, resolver=resolver).fetch(ACCOUNT_DOC)
        finally:
            event.set()
        self.assertLess(time.monotonic() - start, .8)

    def test_private_dns_rejected_without_connection(self):
        def resolver(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with self.assertRaisesRegex(ConnectionPause, '公网'):
            OfficialDocumentReader(resolver=resolver).fetch(ACCOUNT_DOC)


if __name__ == '__main__':
    unittest.main()
