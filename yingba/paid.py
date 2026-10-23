"""Integration seam only. No live Apollo transport is shipped or invoked."""
from dataclasses import dataclass
from datetime import datetime,timezone
import hashlib
import json
from .budget import Budget,BudgetPause
from .adapters import ConnectionPause,UnknownOutcome

@dataclass(frozen=True)
class Receipt:
    payload: object
    actual_points: int
    external_reference: str
    billed_day: str | None = None

class PaidGateway:
    def __init__(self,store,transport=None,verified=False):
        self.store=store;self.budget=Budget(store);self.transport=transport;self.verified=verified
    @staticmethod
    def fingerprint(request):
        return hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()
    def execute(self,action,estimate,request):
        if not self.verified or self.transport is None:raise ConnectionPause('付费接口未验收连接，未预留或执行')
        fingerprint=self.fingerprint(request)
        cached=self.store.rows('SELECT * FROM paid_results WHERE action=?',(action,))
        if cached:
            if cached[0]['request_hash']!=fingerprint:raise BudgetPause('同动作ID对应不同请求，拒绝执行')
            return json.loads(cached[0]['payload'])
        self.budget.reserve(action,estimate)
        # Mark before transport: a crash at any later point requires reconciliation.
        self.budget.unknown(action)
        try:
            receipt=self.transport(action,request)
            return self.reconcile(action,request,receipt)
        except Exception:
            # Never retry a transport or leak its raw error/credentials.
            raise UnknownOutcome('付费动作结果未知，已冻结；先凭服务商回执对账，禁止盲重试') from None
    def reconcile(self,action,request,receipt):
        """Persist a reliable receipt and settlement atomically, without any network call."""
        if not isinstance(receipt,Receipt) or not isinstance(receipt.external_reference,str) or not receipt.external_reference.strip():raise ValueError('缺可靠回执')
        payload=json.dumps(receipt.payload,ensure_ascii=False,allow_nan=False)
        fingerprint=self.fingerprint(request)
        with self.store.transaction() as db:
            prior=db.execute('SELECT * FROM paid_results WHERE action=?',(action,)).fetchone()
            if prior and (prior['request_hash']!=fingerprint or prior['payload']!=payload or prior['external_reference']!=receipt.external_reference):raise BudgetPause('回执冲突')
            self.budget._settle(db,action,receipt.actual_points,receipt.billed_day)
            db.execute('INSERT OR IGNORE INTO paid_results VALUES(?,?,?,?,?)',(action,fingerprint,payload,receipt.external_reference,datetime.now(timezone.utc).isoformat()))
        return json.loads(payload)
