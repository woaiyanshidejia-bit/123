"""Integration seam only. No live Apollo transport is shipped or invoked."""
from dataclasses import dataclass
from .budget import Budget,BudgetPause
from .adapters import ConnectionPause,UnknownOutcome

@dataclass(frozen=True)
class Receipt:
    payload: object
    actual_points: int
    external_reference: str

class PaidGateway:
    def __init__(self,store,transport=None,verified=False):
        self.budget=Budget(store);self.transport=transport;self.verified=verified
    def execute(self,action,estimate,request):
        if not self.verified or self.transport is None:raise ConnectionPause('付费接口未验收连接，未预留或执行')
        self.budget.reserve(action,estimate)
        # Mark before transport: a crash at any later point requires reconciliation.
        self.budget.unknown(action)
        try:
            receipt=self.transport(action,request)
            if not isinstance(receipt,Receipt) or not receipt.external_reference:raise ValueError('缺可靠回执')
            self.budget.settle(action,receipt.actual_points)
        except Exception as exc:
            # Never retry a transport or leak its raw error/credentials.
            raise UnknownOutcome('付费动作结果未知，已冻结；先凭服务商回执对账，禁止盲重试') from None
        return receipt.payload
