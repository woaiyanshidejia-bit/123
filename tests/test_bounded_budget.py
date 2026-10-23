"""Local-only verified-ceiling batch tests. No real service/model is called."""
import concurrent.futures
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from yingba.adapters import ConnectionPause, UnknownOutcome
from yingba.budget import Budget, BudgetPause, beijing_day
from yingba.db import Store
from yingba.paid import PaidGateway, BoundedObservation, Observation, Receipt


class BoundedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / '批次 账本.db')
        self.budget = Budget(self.store)
        self.budget.confirm(0)
        self.calls = []
        self.gateway = PaidGateway(self.store, self.transport, True)
        self.policy = 'synthetic-policy'
        self.gateway.authorize_bounded(self.policy, 'fixture-account', self.request(), 10,
                                       {'evidence': 'synthetic verified account plan only'})

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, page=1):
        return {'provider': 'apollo', 'endpoint': '/fixture/search', 'account_label': 'fixture-account',
                'query': {'page': page, 'per_page': 100, 'keywords': ['lubricants']}}

    def transport(self, action, request):
        self.calls.append(action)
        return BoundedObservation({'records': [{'page': request['query']['page']}], 'exhausted': False})

    def run_page(self, page=1, gateway=None, authorization=None):
        return (gateway or self.gateway).execute_bounded(f'page-{page}', 10, self.request(page),
            authorization=authorization or self.policy, response_validator=lambda p: p['records'][0]['page'] == page)

    def test_verified_pages_hold_ceiling_without_inventing_actual_cost(self):
        for page in range(1, 6):
            self.run_page(page)
        rows = self.store.rows('SELECT * FROM charges')
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r['status'] == 'observed_pending' and r['actual'] is None for r in rows))
        self.assertEqual(sum(r['estimate'] for r in rows), 50)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
        self.assertFalse(self.store.rows('SELECT * FROM charge_settlements'))
        with self.assertRaises(BudgetPause):
            self.run_page(6)
        self.assertEqual(len(self.calls), 5)

    def test_settlement_releases_only_difference_between_ceiling_and_actual(self):
        self.run_page(1)
        self.gateway.reconcile_captured('page-1', 3, beijing_day(), 'r1')
        for page in range(2, 6):
            self.run_page(page)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 3)
        self.assertEqual(sum(r['estimate'] for r in self.gateway.pending()), 40)
        with self.assertRaises(BudgetPause):
            self.run_page(6)

    def test_pending_data_replays_offline_after_restart_without_new_http(self):
        result = self.run_page()
        restarted = PaidGateway(Store(self.store.path))
        self.assertEqual(self.run_page(gateway=restarted), result)
        self.assertEqual(self.calls, ['page-1'])
        metadata = restarted.pending()[0]
        self.assertEqual(metadata['response_verified'], 1)
        self.assertEqual(metadata['policy_id'], self.policy)
        self.assertEqual(metadata['status'], 'observed_pending')

    def test_receipted_data_still_replays_after_pending_settlement(self):
        result = self.run_page()
        self.gateway.reconcile_captured('page-1', 4, beijing_day(), 'r1')
        self.assertEqual(self.run_page(gateway=PaidGateway(Store(self.store.path))), result)
        self.assertEqual(self.gateway.pending(), [])
        self.assertEqual(self.calls, ['page-1'])

    def test_request_template_binding_only_allows_page_to_vary(self):
        for field, value in [('account_label', 'different-account'), ('endpoint', '/other')]:
            req = self.request()
            req[field] = value
            with self.subTest(field=field), self.assertRaises(BudgetPause):
                self.gateway.execute_bounded('wrong', 10, req, authorization=self.policy, response_validator=lambda p: True)
        for field, value in [('per_page', 50), ('keywords', ['software']), ('page', 501), ('page', True)]:
            req = self.request()
            req['query'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(BudgetPause):
                self.gateway.execute_bounded('wrong', 10, req, authorization=self.policy, response_validator=lambda p: True)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.store.rows('SELECT * FROM charges'))

    def test_replay_rejects_other_policy_and_request(self):
        self.gateway.authorize_bounded('other-policy', 'fixture-account', self.request(), 10, {'evidence': 'other valid fixture'})
        self.run_page()
        with self.assertRaises(BudgetPause):
            self.run_page(authorization='other-policy')
        with self.assertRaises(BudgetPause):
            self.gateway.execute_bounded('page-1', 10, self.request(2), authorization=self.policy, response_validator=lambda p: True)
        self.assertEqual(self.calls, ['page-1'])

    def test_unknown_transport_freezes_all_new_requests(self):
        def lost(*args):
            raise OSError('synthetic failure')
        gateway = PaidGateway(self.store, lost, True)
        with self.assertRaises(UnknownOutcome):
            self.run_page(gateway=gateway)
        with self.assertRaises(BudgetPause):
            self.run_page(2)
        with self.assertRaises(BudgetPause):
            self.run_page()
        self.assertEqual(self.calls, [])
        self.assertEqual(gateway.pending()[0]['status'], 'unknown')

    def test_invalid_response_is_captured_without_promotion_even_permissive_validator(self):
        for index, payload in enumerate((
            {'records': None, 'exhausted': None, 'response_state': 'schema_review_required'},
            {'records': [], 'exhausted': False, 'schema_review_required': True},
            {'records': [], 'exhausted': False, 'actual_points': 10},
        )):
            gateway = PaidGateway(self.store, lambda *a, p=payload: BoundedObservation(p), True)
            with self.assertRaises(UnknownOutcome):
                gateway.execute_bounded(f'bad-{index}', 10, self.request(), authorization=self.policy, response_validator=lambda p: True)
            pending = gateway.pending()[0]
            self.assertEqual(pending['status'], 'unknown')
            self.assertEqual(pending['response_saved'], 1)
            self.assertEqual(pending['response_verified'], 0)
            self.budget.settle(f'bad-{index}', 0)

    def test_external_validator_failure_cannot_qualify_response(self):
        for index, validator in enumerate((lambda p: False, lambda p: 1, lambda p: (_ for _ in ()).throw(ValueError('schema')))):
            with self.assertRaises(UnknownOutcome):
                self.gateway.execute_bounded(f'bad-{index}', 10, self.request(), authorization=self.policy, response_validator=validator)
            self.assertEqual(self.gateway.pending()[0]['status'], 'unknown')
            self.assertEqual(self.gateway.pending()[0]['response_saved'], 1)
            self.budget.settle(f'bad-{index}', 0)

    def test_plain_observation_or_receipt_is_not_a_bounded_observation(self):
        for index, response in enumerate((Observation({'records': [], 'exhausted': False}), Receipt({'records': [], 'exhausted': False}, 10, 'r-fixture'))):
            gateway = PaidGateway(self.store, lambda *a, r=response: r, True)
            with self.assertRaises(UnknownOutcome):
                gateway.execute_bounded(f'wrong-{index}', 10, self.request(), authorization=self.policy, response_validator=lambda p: True)
            self.assertEqual(gateway.pending()[0]['status'], 'unknown')
            self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
            self.budget.settle(f'wrong-{index}', 0)

    def test_financial_reconciliation_cannot_approve_unknown_business_schema(self):
        payload = {'records': None, 'exhausted': None, 'response_state': 'schema_review_required'}
        gateway = PaidGateway(self.store, lambda *a: BoundedObservation(payload), True)
        with self.assertRaises(UnknownOutcome):
            gateway.execute_bounded('bad', 10, self.request(), authorization=self.policy, response_validator=lambda p: True)
        gateway.reconcile_captured('bad', 3, beijing_day(), 'r-bad')
        with self.assertRaises(BudgetPause):
            gateway.execute_bounded('bad', 10, self.request(), authorization=self.policy, response_validator=lambda p: True)
        with self.assertRaises(BudgetPause):
            gateway.execute('bad', 10, self.request())

    def test_reconciled_response_still_needs_original_failed_validator_to_pass(self):
        with self.assertRaises(UnknownOutcome):
            self.gateway.execute_bounded('bad', 10, self.request(), authorization=self.policy, response_validator=lambda p: False)
        self.gateway.reconcile_captured('bad', 3, beijing_day(), 'r-bad')
        for validator in (None, lambda p: False):
            with self.assertRaises(BudgetPause):
                self.gateway.execute_bounded('bad', 10, self.request(), authorization=self.policy, response_validator=validator)
        result = PaidGateway(Store(self.store.path)).execute_bounded('bad', 10, self.request(), authorization=self.policy, response_validator=lambda p: p['records'] == [{'page': 1}])
        self.assertEqual(result['records'], [{'page': 1}])
        self.assertEqual(self.calls, ['bad'])
        self.assertIsNotNone(self.store.rows('SELECT observed_at FROM bounded_actions')[0]['observed_at'])

    def test_authorization_requires_known_baseline_and_no_old_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'new.db')
            gateway = PaidGateway(store, self.transport, True)
            with self.assertRaises(BudgetPause):
                gateway.authorize_bounded('new', 'fixture-account', self.request(), 10, {'evidence': 'fixture'})
        self.budget.reserve('legacy', 1)
        self.budget.unknown('legacy')
        with self.assertRaises(BudgetPause):
            self.gateway.authorize_bounded('new', 'fixture-account', self.request(), 10, {'evidence': 'fixture'})
        with self.assertRaises(BudgetPause):
            self.run_page()

    def test_unknown_or_noninteger_ceiling_is_not_authorized(self):
        for ceiling in (None, True, 1.5, -1, 51):
            with self.subTest(ceiling=ceiling), self.assertRaises(BudgetPause):
                self.gateway.authorize_bounded('bad', 'fixture-account', self.request(), ceiling, {'evidence': 'fixture'})
        with self.assertRaises(BudgetPause):
            self.gateway.authorize_bounded('bad', 'fixture-account', self.request(), 10, {})
        with self.assertRaises(BudgetPause):
            self.gateway.execute_bounded('bad', 9, self.request(), authorization=self.policy, response_validator=lambda p: True)
        self.assertEqual(self.calls, [])

    def test_no_validator_or_connection_means_no_reservation(self):
        with self.assertRaises(ConnectionPause):
            self.gateway.execute_bounded('bad', 10, self.request(), authorization=self.policy, response_validator=None)
        with self.assertRaises(ConnectionPause):
            self.run_page(gateway=PaidGateway(self.store))
        self.assertFalse(self.store.rows('SELECT * FROM charges'))

    def test_pending_prevents_baseline_reset_and_new_policy(self):
        self.run_page()
        with self.assertRaises(BudgetPause):
            self.budget.confirm(0)
        with self.assertRaises(BudgetPause):
            self.gateway.authorize_bounded('new', 'fixture-account', self.request(), 10, {'evidence': 'fixture'})
        # Idempotent storage of the original authorization remains harmless.
        self.assertEqual(self.gateway.authorize_bounded(self.policy, 'fixture-account', self.request(), 10,
            {'evidence': 'synthetic verified account plan only'}), self.policy)

    def test_idempotent_authorization_cannot_hide_unknown_usage_or_baseline(self):
        def authorize():
            return self.gateway.authorize_bounded(self.policy, 'fixture-account', self.request(), 10,
                {'evidence': 'synthetic verified account plan only'})
        with self.store.transaction() as db:
            db.execute('UPDATE budgets SET confirmed=0')
        with self.assertRaises(BudgetPause):
            authorize()
        self.budget.confirm(0)
        self.budget.reserve('old', 1)
        self.budget.unknown('old')
        with self.assertRaises(BudgetPause):
            authorize()

    def test_cross_day_pending_is_not_forgotten_and_old_data_remains_readable(self):
        self.run_page()
        future = datetime(2099, 1, 2, tzinfo=timezone.utc)
        with patch('yingba.budget.utc_now', return_value=future):
            with self.assertRaises(BudgetPause):
                self.run_page(2)
            with self.assertRaises(BudgetPause):
                self.budget.confirm(0)
            # Even a synthetic corrupted/externally inserted new baseline must
            # not bypass the old-day held-charge check in the ordinary ledger.
            with self.store.transaction() as db:
                db.execute('INSERT INTO budgets VALUES(?,0,1)', (beijing_day(),))
            with self.assertRaises(BudgetPause):
                self.budget.reserve('new-day', 1)
            self.assertEqual(self.run_page(gateway=PaidGateway(self.store))['records'], [{'page': 1}])

    def test_expired_policy_cannot_dispatch_after_all_prior_charges_settle(self):
        self.run_page()
        self.gateway.reconcile_captured('page-1', 3, beijing_day(), 'r1')
        future = datetime(2099, 1, 2, tzinfo=timezone.utc)
        with patch('yingba.budget.utc_now', return_value=future):
            self.budget.confirm(0)
            with self.assertRaises(BudgetPause):
                self.run_page(2)
            self.gateway.authorize_bounded('new-day', 'fixture-account', self.request(), 10, {'evidence': 'new-day fixture'})
            self.run_page(2, authorization='new-day')

    def test_actual_above_upper_bound_revokes_policy_and_budget_confirmation(self):
        self.run_page()
        self.gateway.reconcile_captured('page-1', 11, beijing_day(), 'r1')
        self.assertEqual(self.store.rows('SELECT used,confirmed FROM budgets')[0], {'used': 11, 'confirmed': 0})
        self.assertEqual(self.store.rows('SELECT confirmed FROM paid_policies')[0]['confirmed'], 0)
        self.budget.confirm(11)
        with self.assertRaises(BudgetPause):
            self.run_page(2)
        self.assertEqual(self.calls, ['page-1'])

    def test_mutated_policy_or_observation_blocks_replay(self):
        self.run_page()
        with self.store.transaction() as db:
            db.execute("UPDATE paid_policies SET evidence='{}'")
        with self.assertRaises(BudgetPause):
            self.run_page()
        self.assertEqual(self.calls, ['page-1'])

    def test_old_api_cannot_bypass_bounded_authorization_checks(self):
        self.run_page()
        self.gateway.reconcile_captured('page-1', 3, beijing_day(), 'r1')
        with self.assertRaises(BudgetPause):
            self.gateway.execute('page-1', 10, self.request())
        with self.store.transaction() as db:
            db.execute("UPDATE paid_policies SET evidence='{}'")
        with self.assertRaises(BudgetPause):
            self.gateway.execute('page-1', 10, self.request())
        with self.assertRaises(BudgetPause):
            self.run_page()
        self.assertEqual(self.calls, ['page-1'])

    def test_concurrent_same_action_dispatches_at_most_once(self):
        def call(_):
            try:
                return self.run_page(gateway=PaidGateway(Store(self.store.path), self.transport, True))
            except BudgetPause:
                return 'paused'
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(20)))
        self.assertEqual(self.calls, ['page-1'])
        self.assertTrue(any(isinstance(r, dict) for r in results))
        self.assertEqual(len(self.gateway.pending()), 1)

    def test_concurrent_reservations_include_pending_upper_bounds(self):
        self.run_page()
        self.run_page(2)
        def reserve(index):
            try:
                Budget(Store(self.store.path)).reserve(f'reserved-{index}', 10)
                return True
            except BudgetPause:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(sum(r['estimate'] for r in self.gateway.pending()), 50)

    def test_process_crash_after_observed_result_replays_without_http(self):
        code = '''import json,os,sys
from yingba.db import Store
from yingba.paid import PaidGateway,BoundedObservation
request=json.loads(sys.argv[2])
g=PaidGateway(Store(sys.argv[1]),lambda *a:BoundedObservation({'records':[], 'exhausted':True}),True)
g.execute_bounded('child',10,request,authorization='synthetic-policy',response_validator=lambda p:True)
os._exit(31)
'''
        result = subprocess.run([sys.executable, '-c', code, self.store.path, json.dumps(self.request())], capture_output=True)
        self.assertEqual(result.returncode, 31, result.stderr.decode())
        replay = PaidGateway(Store(self.store.path)).execute_bounded('child', 10, self.request(), authorization=self.policy, response_validator=None)
        self.assertEqual(replay, {'records': [], 'exhausted': True})
        self.assertEqual(self.gateway.pending()[0]['status'], 'observed_pending')
        self.assertIsNone(self.store.rows('SELECT actual FROM charges')[0]['actual'])

    def test_crash_during_validator_keeps_saved_response_unknown(self):
        code = '''import json,os,sys
from yingba.db import Store
from yingba.paid import PaidGateway,BoundedObservation
request=json.loads(sys.argv[2])
g=PaidGateway(Store(sys.argv[1]),lambda *a:BoundedObservation({'records':[], 'exhausted':True}),True)
g.execute_bounded('child',10,request,authorization='synthetic-policy',response_validator=lambda p:os._exit(32))
'''
        result = subprocess.run([sys.executable, '-c', code, self.store.path, json.dumps(self.request())], capture_output=True)
        self.assertEqual(result.returncode, 32, result.stderr.decode())
        pending = self.gateway.pending()[0]
        self.assertEqual(pending['status'], 'unknown')
        self.assertEqual(pending['response_saved'], 1)
        with self.assertRaises(BudgetPause):
            self.run_page(2)
        with self.assertRaises(BudgetPause):
            self.gateway.execute_bounded('child', 10, self.request(), authorization=self.policy, response_validator=lambda p: True)

    def test_cross_midnight_response_cannot_promote_after_policy_expires(self):
        future = datetime(2099, 1, 2, tzinfo=timezone.utc)
        def validator(payload):
            # Simulate the clock advancing after transport, before promotion.
            self.clock = patch('yingba.budget.utc_now', return_value=future)
            self.clock.start()
            self.addCleanup(self.clock.stop)
            return True
        with self.assertRaises(UnknownOutcome):
            self.gateway.execute_bounded('midnight', 10, self.request(), authorization=self.policy, response_validator=validator)
        self.assertEqual(self.gateway.pending()[0]['status'], 'unknown')
        self.assertEqual(self.gateway.pending()[0]['response_saved'], 1)

    def test_validator_mutation_does_not_change_replayed_payload(self):
        def validator(payload):
            payload['records'] = [{'forged': True}]
            return True
        result = self.gateway.execute_bounded('immutable', 10, self.request(), authorization=self.policy, response_validator=validator)
        self.assertEqual(result['records'], [{'page': 1}])

    def batch(self, actions, actual=4, reference='synthetic account statement', **kwargs):
        return self.gateway.reconcile_verified_batch(actions, actual, beijing_day(), reference,
            confirmed_each=True, **kwargs)

    def test_batch_settlement_is_atomic_and_keeps_original_shared_evidence(self):
        for page in range(1, 4):
            self.run_page(page)
        result = self.batch(['page-3', 'page-1', 'page-2'])
        self.assertEqual(result['actions'], 3)
        self.assertEqual(result['actual_total'], 12)
        self.assertEqual(self.gateway.pending(), [])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 12)
        references = self.store.rows('SELECT external_reference FROM paid_results')
        self.assertEqual(len({r['external_reference'] for r in references}), 3)
        audit = self.store.rows('SELECT * FROM paid_batch_reconciliations')[0]
        self.assertEqual(audit['reference'], 'synthetic account statement')
        self.assertEqual(json.loads(audit['actions']), ['page-1', 'page-2', 'page-3'])
        with self.assertRaises(BudgetPause):
            self.batch(['page-1', 'page-2', 'page-3'])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 12)

    def test_batch_requires_explicit_verified_equal_actuals(self):
        self.run_page()
        for confirmation in (False, None, 'true', 1):
            with self.subTest(confirmation=confirmation), self.assertRaises(BudgetPause):
                self.gateway.reconcile_verified_batch(['page-1'], 4, beijing_day(), 'r1', confirmed_each=confirmation)
        for actions in ([], 'page-1', ['page-1', 'page-1'], ['page-1', 'missing']):
            with self.subTest(actions=actions), self.assertRaises((BudgetPause, ValueError)):
                self.batch(actions)
        for actual in (None, True, -1, 1.5):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                self.batch(['page-1'], actual=actual)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertEqual(self.gateway.pending()[0]['status'], 'observed_pending')

    def test_batch_cannot_include_unknown_result_or_different_policy(self):
        self.gateway.authorize_bounded('second', 'fixture-account', self.request(), 10, {'evidence': 'second fixture'})
        self.run_page(1)
        self.run_page(2, authorization='second')
        with self.assertRaises(BudgetPause):
            self.batch(['page-1', 'page-2'])
        gateway = PaidGateway(self.store, lambda *a: (_ for _ in ()).throw(OSError('fixture')), True)
        with self.assertRaises(UnknownOutcome):
            self.run_page(3, gateway=gateway)
        with self.assertRaises(BudgetPause):
            self.batch(['page-1', 'page-3'])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))

    def test_batch_rejects_corrupted_response_before_any_settlement(self):
        self.run_page(1)
        self.run_page(2)
        with self.store.transaction() as db:
            db.execute("UPDATE paid_observations SET payload='{}' WHERE action='page-2'")
        with self.assertRaises(BudgetPause):
            self.batch(['page-1', 'page-2'])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertEqual(len(self.gateway.pending()), 2)
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))

    def test_batch_mid_transaction_failure_rolls_back_every_action_and_audit(self):
        self.run_page(1)
        self.run_page(2)
        original = self.gateway.budget._settle
        def fail_second(db, action, actual, day=None):
            original(db, action, actual, day)
            if action == 'page-2':
                raise RuntimeError('synthetic failure after second settlement')
        with patch.object(self.gateway.budget, '_settle', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.batch(['page-1', 'page-2'])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertTrue(all(r['status'] == 'observed_pending' for r in self.gateway.pending()))
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
        self.assertFalse(self.store.rows('SELECT * FROM charge_settlements'))
        self.assertFalse(self.store.rows('SELECT * FROM paid_batch_reconciliations'))

    def test_batch_preserves_existing_per_action_provider_reference(self):
        gateway = PaidGateway(self.store, lambda action, req: BoundedObservation(
            {'records': [{'page': req['query']['page']}], 'exhausted': False}, 'provider-' + action), True)
        self.run_page(1, gateway=gateway)
        self.run_page(2, gateway=gateway)
        self.batch(['page-1', 'page-2'])
        refs = {r['external_reference'] for r in self.store.rows('SELECT external_reference FROM paid_results')}
        self.assertEqual(refs, {'provider-page-1', 'provider-page-2'})
        self.assertEqual(self.store.rows('SELECT reference FROM paid_batch_reconciliations')[0]['reference'], 'synthetic account statement')

    def test_batch_duplicate_provider_reference_rolls_back_first_settlement(self):
        gateway = PaidGateway(self.store, lambda action, req: BoundedObservation(
            {'records': [{'page': req['query']['page']}], 'exhausted': False}, 'duplicate-provider-id'), True)
        self.run_page(1, gateway=gateway)
        self.run_page(2, gateway=gateway)
        with self.assertRaises(BudgetPause):
            self.batch(['page-1', 'page-2'])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertEqual(len(self.gateway.pending()), 2)
        self.assertFalse(self.store.rows('SELECT * FROM paid_batch_reconciliations'))

    def test_batch_invalid_day_and_missing_verification_marker_are_rejected(self):
        self.run_page(1)
        self.run_page(2)
        with self.assertRaises(BudgetPause):
            self.gateway.reconcile_verified_batch(['page-1', 'page-2'], 3, '2099-01-02', 'future', confirmed_each=True)
        with self.store.transaction() as db:
            db.execute("UPDATE bounded_actions SET observed_at=NULL WHERE action='page-2'")
        with self.assertRaises(BudgetPause):
            self.batch(['page-1', 'page-2'])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)

    def test_batch_above_verified_ceiling_records_truth_then_revokes_policy(self):
        self.run_page(1)
        self.run_page(2)
        result = self.batch(['page-1', 'page-2'], actual=11)
        self.assertEqual(result['actual_total'], 22)
        self.assertEqual(self.store.rows('SELECT used,confirmed FROM budgets')[0], {'used': 22, 'confirmed': 0})
        self.assertEqual(self.store.rows('SELECT confirmed FROM paid_policies')[0]['confirmed'], 0)
        self.assertTrue(all(row['actual'] == 11 for row in self.store.rows('SELECT actual FROM charges')))
        self.assertEqual(self.gateway.pending(), [])

    def test_concurrent_batch_submission_settles_only_once(self):
        self.run_page(1)
        self.run_page(2)
        def call(_):
            try:
                PaidGateway(Store(self.store.path)).reconcile_verified_batch(['page-1', 'page-2'], 4,
                    beijing_day(), 'synthetic account statement', confirmed_each=True)
                return True
            except BudgetPause:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(call, range(12)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 8)
        self.assertEqual(len(self.store.rows('SELECT * FROM paid_batch_reconciliations')), 1)


if __name__ == '__main__':
    unittest.main()
