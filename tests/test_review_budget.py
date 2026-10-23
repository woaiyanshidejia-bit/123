"""Round-two adversarial ledger tests. All transports are local synthetic mocks."""
import concurrent.futures
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from yingba.adapters import UnknownOutcome
from yingba.budget import Budget, BudgetPause, beijing_day
from yingba.db import Store
from yingba.paid import Observation, PaidGateway, Receipt


class BudgetReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / '中文 账本.db'
        self.store = Store(self.path)
        self.budget = Budget(self.store)
        self.budget.confirm(0)

    def tearDown(self):
        self.tmp.cleanup()

    def unknown(self, action='a', request=None):
        request = request if request is not None else {'page': 1}
        gateway = PaidGateway(self.store, lambda *args: (_ for _ in ()).throw(OSError('fixture only')), True)
        with self.assertRaises(UnknownOutcome):
            gateway.execute(action, 5, request)
        return gateway

    def test_unknown_request_fingerprint_cannot_be_rebound(self):
        gateway = self.unknown()
        with self.assertRaises(BudgetPause):
            gateway.reconcile('a', {'page': 2}, Receipt({'page': 2}, 1, 'wrong-request'))
        self.assertEqual(self.store.rows('SELECT status FROM charges')[0]['status'], 'unknown')
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)

    def test_explicit_receipt_fingerprint_must_match(self):
        gateway = self.unknown()
        with self.assertRaises(BudgetPause):
            gateway.reconcile('a', {'page': 1}, Receipt({}, 1, 'r1', request_hash='wrong'))

    def test_wrong_request_receipt_is_not_captured(self):
        gateway = PaidGateway(self.store, lambda *a: Receipt({'wrong-company': 1}, 1, 'r1', request_hash='wrong'), True)
        with self.assertRaises(UnknownOutcome):
            gateway.execute('wrong', 2, {'page': 1})
        self.assertEqual(gateway.pending()[0]['response_saved'], 0)

    def test_reference_cannot_settle_two_actions(self):
        gateway = PaidGateway(self.store, lambda *a: Receipt({}, 1, 'shared-receipt'), True)
        gateway.execute('first', 2, {})
        with self.assertRaises(UnknownOutcome):
            gateway.execute('second', 2, {})
        self.assertEqual(self.store.rows('SELECT COUNT(*) n FROM paid_results')[0]['n'], 1)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 1)
        self.assertEqual(gateway.pending()[0]['action'], 'second')

    def test_observation_is_saved_but_never_qualified_as_paid_success(self):
        calls = []
        gateway = PaidGateway(self.store, lambda *a: (calls.append(1) or Observation({'synthetic': [1]})), True)
        with self.assertRaises(UnknownOutcome):
            gateway.execute('capture', 3, {'page': 1})
        self.assertEqual(gateway.pending()[0]['response_saved'], 1)
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
        with self.assertRaises(BudgetPause):
            gateway.execute('capture', 3, {'page': 1})
        gateway.reconcile_captured('capture', 2, beijing_day(), 'manual-fixture-receipt')
        replay = PaidGateway(Store(self.path))
        self.assertEqual(replay.execute('capture', 3, {'page': 1}), {'synthetic': [1]})
        self.assertEqual(calls, [1])
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 2)
        self.assertEqual(gateway.pending(), [])

    def test_captured_response_cannot_be_overwritten_or_rebound(self):
        gateway = self.unknown()
        gateway.capture('a', {'page': 1}, {'v': 1}, 'r1')
        with self.assertRaises(BudgetPause):
            gateway.capture('a', {'page': 1}, {'v': 2}, 'r1')
        with self.assertRaises(BudgetPause):
            gateway.capture('a', {'page': 2}, {'v': 1}, 'r1')
        with self.assertRaises(BudgetPause):
            gateway.reconcile_captured('a', 2, beijing_day(), 'r2')
        with self.assertRaises(BudgetPause):
            gateway.reconcile('a', {'page': 1}, Receipt({'v': 2}, 2, 'r1'))

    def test_manual_reconciliation_needs_saved_response_and_billing_day(self):
        gateway = self.unknown()
        with self.assertRaises(BudgetPause):
            gateway.reconcile_captured('a', 2, beijing_day(), 'r1')
        gateway.capture('a', {'page': 1}, {})
        with self.assertRaises(BudgetPause):
            gateway.reconcile_captured('a', 2, None, 'r1')
        with self.assertRaises(ValueError):
            gateway.reconcile_captured('a', None, beijing_day(), 'r1')
        with self.assertRaises(ValueError):
            gateway.reconcile_captured('a', 2, beijing_day(), '')

    def test_capture_hash_and_cached_payload_corruption_pause(self):
        gateway = PaidGateway(self.store, lambda *a: Receipt({'synthetic': 1}, 1, 'r1'), True)
        gateway.execute('a', 2, {})
        with self.store.transaction() as db:
            db.execute('UPDATE paid_results SET payload=? WHERE action=?', ('{"synthetic":2}', 'a'))
        with self.assertRaises(BudgetPause):
            gateway.execute('a', 2, {})

    def test_capture_payload_checksum_checked_during_manual_reconcile(self):
        gateway = self.unknown()
        gateway.capture('a', {'page': 1}, {})
        with self.store.transaction() as db:
            db.execute('UPDATE paid_observations SET payload=?', ('{"forged":1}',))
        with self.assertRaises(BudgetPause):
            gateway.reconcile_captured('a', 2, beijing_day(), 'r1')

    def test_receipt_dict_key_order_does_not_double_charge(self):
        gateway = PaidGateway(self.store, lambda *a: Receipt({'a': 1, 'b': 2}, 2, 'r1'), True)
        gateway.execute('a', 3, {'page': 1, 'country': 'CN'})
        gateway.reconcile('a', {'country': 'CN', 'page': 1}, Receipt({'b': 2, 'a': 1}, 2, 'r1'))
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 2)

    def test_gateway_request_snapshot_is_not_changed_by_transport_mutation(self):
        request = {'page': 1}
        def transport(action, value):
            value['page'] = 99
            return Receipt({'ok': True}, 1, 'r1')
        gateway = PaidGateway(self.store, transport, True)
        gateway.execute('a', 2, request)
        self.assertEqual(request, {'page': 1})
        self.assertEqual(json.loads(self.store.rows('SELECT request_json FROM paid_requests')[0]['request_json']), request)
        with self.assertRaises(BudgetPause):
            gateway.execute('a', 2, {'page': 99})

    def test_concurrent_same_action_calls_transport_once(self):
        count = []
        lock = threading.Lock()
        def transport(*args):
            with lock:
                count.append(1)
            return Receipt({'ok': True}, 1, 'r1')
        def call(_):
            try:
                return PaidGateway(Store(self.path), transport, True).execute('same', 2, {})
            except BudgetPause:
                return 'pending'
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(call, range(24)))
        self.assertEqual(count, [1])
        self.assertIn({'ok': True}, outcomes)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 1)

    def test_concurrent_manual_reconciliation_charges_once(self):
        gateway = self.unknown()
        gateway.capture('a', {'page': 1}, {'synthetic': 1})
        def call(_):
            return PaidGateway(Store(self.path)).reconcile_captured('a', 3, beijing_day(), 'r1')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(16)))
        self.assertEqual(len(results), 16)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 3)
        self.assertEqual(len(self.store.rows('SELECT * FROM paid_results')), 1)

    def test_independent_processes_share_one_atomic_fifty_point_limit(self):
        code = '''import sys
from yingba.db import Store
from yingba.budget import Budget,BudgetPause
try:Budget(Store(sys.argv[1])).reserve(sys.argv[2],10)
except BudgetPause:sys.exit(3)
'''
        def run(index):
            return subprocess.run([sys.executable, '-c', code, str(self.path), f'process-{index}'], capture_output=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(run, range(12)))
        self.assertEqual(sum(p.returncode == 0 for p in results), 5)
        self.assertEqual(sum(p.returncode == 3 for p in results), 7)
        self.assertEqual(self.store.rows('SELECT SUM(estimate) held FROM charges')[0]['held'], 50)

    def test_process_exit_after_settlement_replays_without_transport(self):
        code = '''import os,sys
from yingba.db import Store
from yingba.paid import PaidGateway,Receipt
PaidGateway(Store(sys.argv[1]),lambda *a:Receipt({'settled':True},2,'r-exit'),True).execute('exit',3,{'page':1})
os._exit(25)
'''
        process = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True)
        self.assertEqual(process.returncode, 25, process.stderr.decode())
        gateway = PaidGateway(Store(self.path))
        self.assertEqual(gateway.execute('exit', 3, {'page': 1}), {'settled': True})
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 2)

    def test_process_crash_after_capture_reconciles_without_network(self):
        code = '''import os,sys
from yingba.db import Store
from yingba.paid import PaidGateway,Observation
from yingba.adapters import UnknownOutcome
g=PaidGateway(Store(sys.argv[1]),lambda *a:Observation({'synthetic':[20]}),True)
try:g.execute('killed',4,{'page':1})
except UnknownOutcome:os._exit(23)
'''
        process = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True)
        self.assertEqual(process.returncode, 23, process.stderr.decode())
        gateway = PaidGateway(Store(self.path))
        self.assertEqual(gateway.pending()[0]['response_saved'], 1)
        gateway.reconcile_captured('killed', 3, beijing_day(), 'r-killed')
        self.assertEqual(gateway.execute('killed', 4, {'page': 1}), {'synthetic': [20]})

    def test_process_crash_inside_transport_keeps_request_binding(self):
        code = '''import os,sys
from yingba.db import Store
from yingba.paid import PaidGateway
PaidGateway(Store(sys.argv[1]),lambda *a:os._exit(24),True).execute('killed',4,{'page':1})
'''
        process = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True)
        self.assertEqual(process.returncode, 24, process.stderr.decode())
        gateway = PaidGateway(Store(self.path), lambda *a: self.fail('must never dispatch'), True)
        self.assertEqual(gateway.pending()[0]['request_hash'], gateway.fingerprint({'page': 1}))
        self.assertEqual(gateway.pending()[0]['response_saved'], 0)
        with self.assertRaises(BudgetPause):
            gateway.execute('killed', 4, {'page': 1})
        with self.assertRaises(BudgetPause):
            gateway.reconcile('killed', {'page': 2}, Receipt({}, 1, 'wrong'))

    def test_failed_settlement_retains_response_and_rolls_back_cost(self):
        gateway = PaidGateway(self.store, lambda *a: Receipt({'ok': True}, 2, 'r1'), True)
        original = gateway.budget._settle
        def fail(db, action, actual, day=None):
            original(db, action, actual, day)
            raise RuntimeError('simulated database failure')
        with patch.object(gateway.budget, '_settle', side_effect=fail):
            with self.assertRaises(UnknownOutcome):
                gateway.execute('a', 3, {})
        self.assertEqual(gateway.pending()[0]['response_saved'], 1)
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 0)
        self.assertFalse(self.store.rows('SELECT * FROM paid_results'))
        gateway.reconcile_captured('a', 2, beijing_day(), 'r1')
        self.assertEqual(self.store.rows('SELECT used FROM budgets')[0]['used'], 2)

    def test_cross_day_observation_blocks_new_baseline_until_reconciled(self):
        before = datetime(2026, 10, 23, 15, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 24, 16, 1, tzinfo=timezone.utc)
        gateway = PaidGateway(self.store, lambda *a: Observation({'synthetic': 1}), True)
        with patch('yingba.budget.utc_now', return_value=before):
            self.budget.confirm(0)
            with self.assertRaises(UnknownOutcome):
                gateway.execute('across-days', 10, {'page': 1})
        with patch('yingba.budget.utc_now', return_value=after):
            with self.assertRaises(BudgetPause):
                self.budget.confirm(0)
            with self.assertRaises(BudgetPause):
                self.budget.reserve('new', 1)
            gateway.reconcile_captured('across-days', 8, '2026-10-24', 'r-days')
            self.budget.confirm(0)
            self.budget.reserve('new', 50)
        self.assertEqual(self.store.rows('SELECT used FROM budgets WHERE day=?', ('2026-10-24',))[0]['used'], 8)

    def test_action_id_validation_prevents_sqlite_null_primary_keys(self):
        for action in (None, '', ' ', True, 1, 'x\n', 'a' * 513):
            with self.subTest(action=action):
                with self.assertRaises(BudgetPause):
                    self.budget.reserve(action, 0)
        self.assertFalse(self.store.rows('SELECT * FROM charges'))

    def test_integer_overflow_cannot_turn_account_used_into_float(self):
        with self.assertRaises(ValueError):
            self.budget.confirm(1 << 64)
        self.budget.reserve('a', 1)
        with self.assertRaises(ValueError):
            self.budget.settle('a', 1 << 64)
        with self.store.transaction() as db:
            db.execute('UPDATE budgets SET used=?', (Budget.MAX_LEDGER_POINTS,))
        with self.assertRaises(BudgetPause):
            self.budget.settle('a', 1)
        self.assertEqual(self.store.rows('SELECT status FROM charges')[0]['status'], 'reserved')

    def test_timezone_and_noncanonical_billing_dates_rejected(self):
        with self.assertRaises(ValueError):
            beijing_day(datetime(2026, 10, 23))
        self.budget.reserve('a', 1)
        for day in ('20261023', '2026-W43-5', 20261023, '2026-1-1'):
            with self.subTest(day=day):
                with self.assertRaises(BudgetPause):
                    self.budget.settle('a', 1, billed_day=day)
        self.assertEqual(self.store.rows('SELECT status FROM charges')[0]['status'], 'reserved')

    def test_confirmations_are_auditable_and_never_reset_spend(self):
        self.budget.confirm(4, basis='synthetic dashboard snapshot')
        with self.assertRaises(BudgetPause):
            self.budget.confirm(0)
        rows = self.store.rows('SELECT * FROM budget_confirmations ORDER BY id')
        self.assertEqual([r['used'] for r in rows], [0, 4])
        self.assertEqual(rows[-1]['basis'], 'synthetic dashboard snapshot')

    def test_legacy_database_migrates_known_hash_without_guessing_unknown(self):
        path = Path(self.tmp.name) / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE charges(action TEXT PRIMARY KEY,day TEXT NOT NULL,estimate INTEGER NOT NULL,actual INTEGER,status TEXT NOT NULL);
CREATE TABLE budgets(day TEXT PRIMARY KEY,used INTEGER NOT NULL,confirmed INTEGER NOT NULL);
CREATE TABLE charge_settlements(action TEXT PRIMARY KEY REFERENCES charges(action),billed_day TEXT NOT NULL);
CREATE TABLE paid_results(action TEXT PRIMARY KEY REFERENCES charges(action),request_hash TEXT NOT NULL,payload TEXT NOT NULL,external_reference TEXT NOT NULL,collected TEXT NOT NULL);''')
            db.execute('INSERT INTO budgets VALUES(?,1,1)', (beijing_day(),))
            db.execute('INSERT INTO charges VALUES(?,?,2,1,?)', ('known', beijing_day(), 'settled'))
            db.execute('INSERT INTO charges VALUES(?,?,2,NULL,?)', ('unknown', beijing_day(), 'unknown'))
            db.execute('INSERT INTO charge_settlements VALUES(?,?)', ('known', beijing_day()))
            db.execute('INSERT INTO paid_results VALUES(?,?,?,?,?)', ('known', PaidGateway.fingerprint({'page': 1}), '{"legacy":true}', 'legacy-receipt', 'legacy'))
        store = Store(path)
        gateway = PaidGateway(store)
        self.assertEqual(gateway.execute('known', 2, {'page': 1}), {'legacy': True})
        with self.assertRaises(BudgetPause):
            gateway.reconcile('unknown', {'page': 9}, Receipt({}, 0, 'guess'))
        self.assertEqual(len(store.rows('SELECT * FROM paid_requests')), 1)
        Budget(store).settle('unknown', 0)
        self.assertEqual(store.rows('PRAGMA user_version')[0]['user_version'], 2)
        Store(path)
        self.assertEqual(len(store.rows('SELECT * FROM paid_requests')), 1)

    def test_pending_metadata_never_contains_request_or_response_body(self):
        gateway = self.unknown(request={'sensitive-business-field': 'synthetic secret'})
        gateway.capture('a', {'sensitive-business-field': 'synthetic secret'}, {'email': 'synthetic@example.invalid'})
        public = json.dumps(gateway.pending())
        self.assertNotIn('synthetic secret', public)
        self.assertNotIn('synthetic@example.invalid', public)

    def test_newer_database_is_not_silently_downgraded(self):
        with self.store.transaction() as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):
            Store(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)


if __name__ == '__main__':
    unittest.main()
