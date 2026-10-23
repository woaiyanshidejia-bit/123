"""Crash-safe transport boundary. Credentials must never be part of request/payload.

A response is not proof of billing. Transports without a reliable per-action
charge receipt return Observation; the response is saved and execution pauses
until manual reconciliation. No path here retries a network request.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from .budget import Budget, BudgetPause, validate_action
from .adapters import ConnectionPause, UnknownOutcome


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def valid_reference(value, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError('缺少有效的动作专属回执依据')
    return value


@dataclass(frozen=True)
class Receipt:
    payload: object
    actual_points: int
    external_reference: str
    billed_day: str | None = None
    request_hash: str | None = None


@dataclass(frozen=True)
class Observation:
    """A captured provider response whose actual charge is still unverified."""
    payload: object
    external_reference: str = ''


class PaidGateway:
    def __init__(self, store, transport=None, verified=False):
        self.store = store
        self.budget = Budget(store)
        self.transport = transport
        self.verified = verified

    @staticmethod
    def fingerprint(request):
        return digest(canonical_json(request))

    @staticmethod
    def _binding(db, action, fingerprint):
        row = db.execute('SELECT * FROM paid_requests WHERE action=?', (action,)).fetchone()
        if not row:
            raise BudgetPause('原动作缺少可验证请求指纹；禁止关联新响应或重发，需人工核对旧账')
        if row['request_hash'] != fingerprint:
            raise BudgetPause('同动作ID对应不同请求，拒绝执行或对账')
        return row

    def _cached(self, db, action, fingerprint):
        row = db.execute('SELECT * FROM paid_results WHERE action=?', (action,)).fetchone()
        if not row:
            return None
        self._binding(db, action, fingerprint)
        charge = db.execute('SELECT * FROM charges WHERE action=?', (action,)).fetchone()
        settlement = db.execute('SELECT billed_day FROM charge_settlements WHERE action=?', (action,)).fetchone()
        if row['request_hash'] != fingerprint or not charge or charge['status'] != 'settled' or charge['actual'] is None or not settlement:
            raise BudgetPause('付费结果与结算账本不一致，禁止继续')
        if db.execute('SELECT 1 FROM paid_results WHERE external_reference=? AND action<>?', (row['external_reference'], action)).fetchone():
            raise BudgetPause('重复回执关联多个动作，需核对历史数据')
        observation = db.execute('SELECT * FROM paid_observations WHERE action=?', (action,)).fetchone()
        if observation and (digest(observation['payload']) != observation['response_hash'] or digest(canonical_json(json.loads(row['payload']))) != observation['response_hash']):
            raise BudgetPause('已保存付费响应校验失败，禁止继续')
        return row

    def execute(self, action, estimate, request):
        validate_action(action)
        request_json = canonical_json(request)
        fingerprint = digest(request_json)
        with self.store.transaction() as db:
            cached = self._cached(db, action, fingerprint)
            if cached:
                return json.loads(cached['payload'])
            if not self.verified or self.transport is None:
                raise ConnectionPause('付费接口未验收连接，未预留或执行')
            self.budget._reserve(db, action, estimate)
            db.execute('INSERT INTO paid_requests VALUES(?,?,?,?)', (action, fingerprint, request_json, datetime.now(timezone.utc).isoformat()))
            # Commit request binding and unknown BEFORE transport. A crash may
            # mean "not sent" or "sent"; both require reconciliation, never retry.
            db.execute("UPDATE charges SET status='unknown' WHERE action=?", (action,))
        try:
            receipt = self.transport(action, json.loads(request_json))
            if isinstance(receipt, Observation):
                self.capture(action, json.loads(request_json), receipt.payload, receipt.external_reference)
                raise UnknownOutcome('已保存服务商响应，实际费用尚未核实；请对账后继续，不会重复请求')
            if isinstance(receipt, Receipt):
                if receipt.request_hash is not None and receipt.request_hash != fingerprint:
                    raise BudgetPause('回执请求指纹不匹配')
                self.capture(action, json.loads(request_json), receipt.payload, receipt.external_reference)
            return self.reconcile(action, json.loads(request_json), receipt)
        except Exception:
            # Raw HTTP errors may contain API keys. Expose a fixed safe message.
            raise UnknownOutcome('付费动作等待对账，已冻结；如响应已保存可直接结算，禁止盲重试') from None

    def capture(self, action, request, payload, external_reference=''):
        """Durably retain an unpriced response. Does not settle or return success."""
        validate_action(action)
        valid_reference(external_reference, required=False)
        serialized = canonical_json(payload)
        fingerprint = self.fingerprint(request)
        with self.store.transaction() as db:
            self._binding(db, action, fingerprint)
            charge = db.execute('SELECT status FROM charges WHERE action=?', (action,)).fetchone()
            if not charge or charge['status'] not in ('unknown', 'settled'):
                raise BudgetPause('动作尚未进入可对账状态')
            prior = db.execute('SELECT * FROM paid_observations WHERE action=?', (action,)).fetchone()
            if prior:
                if prior['response_hash'] != digest(serialized) or prior['external_reference'] != external_reference:
                    raise BudgetPause('已保存响应与新响应冲突，禁止覆盖')
                return
            settled = self._cached(db, action, fingerprint)
            if settled and canonical_json(json.loads(settled['payload'])) != serialized:
                raise BudgetPause('已结算响应与新响应冲突')
            if settled and external_reference and settled['external_reference'] != external_reference:
                raise BudgetPause('已结算回执与新回执冲突')
            db.execute('INSERT INTO paid_observations VALUES(?,?,?,?,?)', (action, serialized, digest(serialized), external_reference, datetime.now(timezone.utc).isoformat()))

    def pending(self):
        """Safe UI metadata; raw responses and request bodies are not exposed."""
        return self.store.rows('''SELECT c.action,c.day,c.estimate,c.status,r.request_hash,
            CASE WHEN o.action IS NULL THEN 0 ELSE 1 END AS response_saved,
            o.collected AS response_collected,o.external_reference
            FROM charges c LEFT JOIN paid_requests r ON r.action=c.action
            LEFT JOIN paid_observations o ON o.action=c.action
            WHERE c.status IN ('reserved','unknown') ORDER BY c.day,c.action''')

    def reconcile_captured(self, action, actual_points, billed_day, external_reference):
        """Use the already captured response, with independently verified billing.

        billed_day and an action-specific receipt reference must be supplied;
        estimate is deliberately not a default for actual_points.
        """
        validate_action(action)
        if billed_day is None:
            raise BudgetPause('请核实实际扣费归属日')
        valid_reference(external_reference)
        with self.store.connect() as db:
            row = db.execute('''SELECT r.request_json,r.request_hash,o.payload,o.response_hash
                FROM paid_requests r JOIN paid_observations o ON o.action=r.action WHERE r.action=?''', (action,)).fetchone()
            if not row or row['request_json'] is None:
                raise BudgetPause('未保存完整响应或原请求，不能自动恢复；请人工核对且勿重发')
            if digest(row['payload']) != row['response_hash']:
                raise BudgetPause('已保存响应校验失败，禁止继续')
            request = json.loads(row['request_json'])
            payload = json.loads(row['payload'])
        return self.reconcile(action, request, Receipt(payload, actual_points, external_reference, billed_day, row['request_hash']))

    def reconcile(self, action, request, receipt):
        """Atomically bind a reliable receipt, settle it and publish its payload."""
        validate_action(action)
        if not isinstance(receipt, Receipt):
            raise ValueError('缺可靠回执；仅收到响应时请保存 Observation')
        valid_reference(receipt.external_reference)
        serialized = canonical_json(receipt.payload)
        fingerprint = self.fingerprint(request)
        if receipt.request_hash is not None and receipt.request_hash != fingerprint:
            raise BudgetPause('回执请求指纹不匹配')
        with self.store.transaction() as db:
            self._binding(db, action, fingerprint)
            observation = db.execute('SELECT * FROM paid_observations WHERE action=?', (action,)).fetchone()
            if observation and (observation['response_hash'] != digest(serialized) or digest(observation['payload']) != observation['response_hash'] or (observation['external_reference'] and observation['external_reference'] != receipt.external_reference)):
                raise BudgetPause('回执与已保存响应不匹配')
            if db.execute('SELECT 1 FROM paid_results WHERE external_reference=? AND action<>?', (receipt.external_reference, action)).fetchone():
                raise BudgetPause('该回执已属于另一动作，禁止重复结算')
            prior = db.execute('SELECT * FROM paid_results WHERE action=?', (action,)).fetchone()
            if prior and (prior['request_hash'] != fingerprint or canonical_json(json.loads(prior['payload'])) != serialized or prior['external_reference'] != receipt.external_reference):
                raise BudgetPause('回执冲突')
            self.budget._settle(db, action, receipt.actual_points, receipt.billed_day)
            db.execute('INSERT OR IGNORE INTO paid_results VALUES(?,?,?,?,?)', (action, fingerprint, serialized, receipt.external_reference, datetime.now(timezone.utc).isoformat()))
        return json.loads(serialized)
