"""Production orchestration with synthetic HTTP, public pages and cost decisions.

These reserved .example companies are test fixtures, never real customer leads.
Runtime, Engine, PaidGateway, SQLite and Apollo parsing execute unchanged; no
Apollo request, model invocation or real credential is used by this module.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from yingba.adapters import ConnectionPause
from yingba.apollo import SEARCH_PATH, build_search_request, parse_search_response
from yingba.budget import Budget, BudgetPause, beijing_day
from yingba.db import Store
from yingba.engine import Engine
from yingba.paid import PaidGateway
from yingba.public_web import PublicVerifier
from yingba.runtime import Runtime, settings
from tests.test_runtime import Fetcher, Reviewer


class SyntheticHTTPClient:
    """Inject the HTTP boundary while retaining the real response parser."""

    def __init__(self, api_key, *, enabled=False):
        if api_key != 'synthetic-bounded-fixture-key' or enabled is not True:
            raise AssertionError('Only explicit synthetic connection is allowed')
        self.calls = []
        self.failure = None

    def health(self):
        return {'healthy': True, 'is_logged_in': True, 'fixture': True}

    def search(self, request, *, authorized=False):
        if authorized is not True:
            raise AssertionError('Missing per-call authorization')
        self.calls.append(deepcopy(request))
        if self.failure == 'unknown':
            raise OSError('Synthetic lost HTTP response after request dispatch')
        page, size, total = request['page'], request['per_page'], 4
        raw = {
            'organizations': [
                {'id': f'fixture-org-{i}', 'name': f'Fixture Oil {i}',
                 'primary_domain': f'oil{i}.example',
                 'website_url': f'https://oil{i}.example'}
                for i in range((page - 1) * size + 1, min(page * size, total) + 1)
            ],
            'accounts': [], 'partial_results_only': False,
            'pagination': {'page': page, 'per_page': size,
                           'total_entries': total, 'total_pages': (total + size - 1) // size},
        }
        if self.failure == 'schema':
            # Official mixed-array pagination remains unconfirmed. Its original
            # response must survive, but cannot become executable paid data.
            raw['accounts'] = [{'id': 'fixture-account-1', 'name': 'Fixture Saved Oil',
                                'domain': 'saved-oil.example'}]
        return parse_search_response(raw, request)


class SyntheticCostService:
    """Replace only the cost assessment, not authorization or budget checks."""

    def __init__(self, store):
        self.store = store
        self.upper_bound = 1
        self.calls = []

    def assess(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if kwargs.get('authorized') is not True or kwargs['source'] != 'apollo_organizations':
            raise ConnectionPause('Synthetic fee assessment was not authorized')
        request = {
            'provider': 'apollo', 'endpoint': SEARCH_PATH,
            'account_label': kwargs['account_label'],
            'query': build_search_request(1, kwargs['per_page'], kwargs['filters']),
        }
        policy_id = uuid.uuid4().hex
        PaidGateway(self.store).authorize_bounded(
            policy_id, kwargs['account_label'], request, self.upper_bound,
            {'basis': 'Synthetic account-specific upper bound; no real tariff assertion',
             'fixture': True},
        )
        return {'classification': 'paid', 'max_points': self.upper_bound,
                'policy_id': policy_id, 'reason': 'Synthetic bounded authorization'}


class BoundedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / '带空格 预算.db')
        self.costs = SyntheticCostService(self.store)
        self.rt = self.runtime(self.store, self.costs)
        self.engine = Engine(self.store, source_factory=self.rt.source_for_task)
        self.config = settings({'per_page': 2, 'account_label': '合成测试账号'})
        self.rt.connect('synthetic-bounded-fixture-key', self.config['account_label'], True)
        self.client = self.rt._client
        Budget(self.store).confirm(0, basis='Synthetic whole-account baseline')
        self.assessment = self.rt.assess_cost(self.config, authorized=True)
        self.config = settings(dict(self.config, cost_policy_id=self.assessment['policy_id'],
                                    estimate_points=self.assessment['max_points']))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def runtime(store, costs=None):
        return Runtime(store, client_factory=SyntheticHTTPClient,
                       verifier=PublicVerifier(Fetcher()), reviewer=Reviewer(),
                       cost_service=costs or SyntheticCostService(store))

    def task(self, target=3, config=None):
        return self.engine.create(target, 'real', config=self.config if config is None else config)

    def source(self, task, runtime=None):
        return (runtime or self.rt).source_for_task({'id': task, 'mode': 'real'})

    def row(self, task):
        return self.store.rows('SELECT * FROM tasks WHERE id=?', (task,))[0]

    def test_two_pages_keep_surplus_without_manual_page_settlement(self):
        task = self.task()
        self.engine.run(task)
        self.assertEqual(self.row(task)['status'], 'complete')
        self.assertEqual(self.engine.counts(task)['合格有邮箱'], 4)
        self.assertEqual(self.engine.counts(task)['距目标差额'], 0)
        self.assertEqual([c['page'] for c in self.client.calls], [1, 2])
        self.assertEqual(len(self.costs.calls), 1)
        charges = self.store.rows('SELECT status,actual,estimate FROM charges')
        self.assertEqual(charges, [{'status': 'observed_pending', 'actual': None, 'estimate': 1}] * 2)
        self.assertEqual(sum(c['estimate'] for c in charges), 2)
        self.assertEqual(self.store.rows('SELECT used FROM budgets'), [{'used': 0}])
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
        self.assertFalse(self.store.rows("SELECT * FROM events WHERE kind='paused'"))
        self.assertEqual(len(self.store.rows('SELECT * FROM emails')), 4)
        self.assertNotIn(b'synthetic-bounded-fixture-key', Path(self.store.path).read_bytes())

    def test_restart_replays_observed_page_without_key_and_finishes_review(self):
        task = self.task(target=1)
        before = self.source(task).search(1)  # Simulate crash before Engine page commit.
        self.assertEqual(self.row(task)['page'], 1)
        self.rt.disconnect()
        restarted = self.runtime(Store(self.store.path))
        self.assertFalse(restarted.status()['health_checked'])
        self.assertEqual(self.source(task, restarted).search(1), before)
        engine = Engine(restarted.store, source_factory=restarted.source_for_task)
        engine.run(task)
        self.assertEqual(self.row(task)['status'], 'complete')
        self.assertEqual(engine.counts(task)['合格有邮箱'], 2)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.store.rows('SELECT actual FROM charges'), [{'actual': None}])

    def test_unknown_http_result_freezes_original_and_other_tasks(self):
        self.client.failure = 'unknown'
        task = self.task()
        self.engine.run(task)
        self.assertEqual(self.row(task)['status'], 'paused')
        self.engine.run(task)
        self.engine.resume(task)
        self.engine.run(task)
        other = self.task()
        self.engine.run(other)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.row(task)['page'], 1)
        self.assertEqual(self.row(other)['status'], 'paused')
        self.assertEqual(self.store.rows('SELECT status,actual FROM charges'),
                         [{'status': 'unknown', 'actual': None}])
        self.assertFalse(self.store.rows('SELECT * FROM paid_observations'))
        self.assertEqual(self.engine.counts(task)['合格有邮箱'], 0)

    def test_schema_unknown_response_is_saved_without_promotion_or_retry(self):
        self.client.failure = 'schema'
        task = self.task()
        self.engine.run(task)
        self.engine.run(task)
        self.engine.resume(task)
        self.engine.run(task)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.row(task)['status'], 'paused')
        self.assertEqual(self.row(task)['page'], 1)
        pending = PaidGateway(self.store).pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'unknown')
        self.assertEqual(pending[0]['response_saved'], 1)
        self.assertEqual(pending[0]['response_verified'], 0)
        saved = json.loads(self.store.rows('SELECT payload FROM paid_observations')[0]['payload'])
        self.assertEqual(saved['response_state'], 'schema_review_required')
        self.assertIsNone(saved['records'])
        self.assertEqual(saved['raw']['accounts'][0]['id'], 'fixture-account-1')
        self.assertFalse(self.store.rows('SELECT * FROM companies'))

    def test_changed_query_or_account_cannot_reuse_authorization(self):
        for changes in ({'per_page': 4}, {'filters': {'q_organization_keyword_tags': ['software']}},
                        {'account_label': '其他测试账号'}, {'estimate_points': 2}):
            with self.subTest(changes=changes):
                task = self.task(config=settings(dict(self.config, **changes)))
                self.engine.run(task)
                self.assertEqual(self.row(task)['status'], 'paused')
        task = self.task()
        self.rt.connect('synthetic-bounded-fixture-key', '不同连接账号', True)
        self.engine.run(task)
        self.assertEqual(self.row(task)['status'], 'paused')
        self.assertFalse(self.client.calls)
        self.assertFalse(self.rt._client.calls)
        self.assertFalse(self.store.rows('SELECT * FROM charges'))

    def test_expired_policy_requires_bound_renewal_before_new_page(self):
        task = self.task()
        self.engine.run(task, max_steps=3)  # Search + review both first-page rows.
        self.assertEqual(self.engine.counts(task)['合格有邮箱'], 2)
        self.assertEqual(self.row(task)['page'], 2)
        action = f'apollo:{task}:page:1'
        PaidGateway(self.store).reconcile_captured(action, 1, beijing_day(), 'synthetic-receipt-1')
        with patch('yingba.budget.utc_now', return_value=datetime(2099, 1, 2, tzinfo=timezone.utc)):
            Budget(self.store).confirm(0, basis='Synthetic renewed daily account baseline')
            self.engine.run(task)
            self.assertEqual(self.row(task)['status'], 'paused')
            self.assertEqual(len(self.client.calls), 1)
            assessment = self.rt.assess_cost(self.config, authorized=True)
            self.rt.bind_cost_policy(task, assessment)
            saved = json.loads(self.store.rows('SELECT config FROM task_settings WHERE task_id=?', (task,))[0]['config'])
            self.assertEqual(saved['cost_policy_id'], assessment['policy_id'])
            self.assertEqual(saved['filters'], self.config['filters'])
            self.engine.resume(task)
            self.engine.run(task)
            self.assertEqual(self.row(task)['status'], 'complete')
            self.assertEqual(self.engine.counts(task)['合格有邮箱'], 4)
        self.assertEqual([c['page'] for c in self.client.calls], [1, 2])
        actions = self.store.rows('SELECT policy_id FROM bounded_actions ORDER BY action')
        self.assertEqual([a['policy_id'] for a in actions], [self.assessment['policy_id'], assessment['policy_id']])

    def test_old_page_uses_original_policy_and_ceiling_after_renewal(self):
        task = self.task()
        original = self.source(task).search(1)
        PaidGateway(self.store).reconcile_captured(f'apollo:{task}:page:1', 1, beijing_day(), 'synthetic-old-receipt')
        self.engine.pause(task)
        self.costs.upper_bound = 2
        with patch('yingba.budget.utc_now', return_value=datetime(2099, 1, 2, tzinfo=timezone.utc)):
            Budget(self.store).confirm(0, basis='Synthetic next-day baseline')
            renewed = self.rt.assess_cost(self.config, authorized=True)
            self.rt.bind_cost_policy(task, renewed)
            self.rt.disconnect()
            restarted = self.runtime(Store(self.store.path))
            self.assertEqual(self.source(task, restarted).search(1), original)
        old = self.store.rows('SELECT a.policy_id,c.estimate FROM bounded_actions a JOIN charges c ON c.action=a.action')[0]
        self.assertEqual(old, {'policy_id': self.assessment['policy_id'], 'estimate': 1})
        self.assertEqual(len(self.client.calls), 1)

    def test_renewal_refuses_running_task_and_mismatched_policy(self):
        task = self.task()
        with self.assertRaises(ConnectionPause):
            self.rt.bind_cost_policy(task, self.assessment)
        self.engine.pause(task)
        other = dict(self.config, filters={'q_organization_keyword_tags': ['software']})
        assessment = self.rt.assess_cost(other, authorized=True)
        with self.assertRaises(BudgetPause):
            self.rt.bind_cost_policy(task, assessment)
        saved = json.loads(self.store.rows('SELECT config FROM task_settings WHERE task_id=?', (task,))[0]['config'])
        self.assertEqual(saved['cost_policy_id'], self.assessment['policy_id'])
        self.assertFalse(self.client.calls)
        self.assertFalse(self.store.rows('SELECT * FROM charges'))


if __name__ == '__main__':
    unittest.main()
