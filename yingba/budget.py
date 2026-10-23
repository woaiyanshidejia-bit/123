"""Fail-closed global account ledger. Points are integer units; no API billing."""
from datetime import datetime, timezone, timedelta
class BudgetPause(Exception): pass

def beijing_day(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8))).date().isoformat()

class Budget:
    LIMIT = 50
    def __init__(self, store): self.store=store
    def confirm(self, used, now=None):
        if not isinstance(used,int) or used<0: raise ValueError('已用量必须为非负整数')
        day=beijing_day(now)
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM charges WHERE status IN ('reserved','unknown')").fetchone():
                raise BudgetPause('先对账所有未结算动作，禁止覆盖账户基线')
            db.execute('INSERT INTO budgets VALUES(?,?,1) ON CONFLICT(day) DO UPDATE SET used=excluded.used,confirmed=1',(day,used))
    def reserve(self, action, estimate, now=None):
        if not isinstance(estimate,int) or estimate<0: raise BudgetPause('预计用量未知')
        day=beijing_day(now)
        with self.store.transaction() as db:
            old=db.execute('SELECT * FROM charges WHERE action=?',(action,)).fetchone()
            if old: raise BudgetPause('相同动作已登记，先查询或对账，禁止重复执行')
            b=db.execute('SELECT * FROM budgets WHERE day=?',(day,)).fetchone()
            if not b or not b['confirmed']: raise BudgetPause('全账号今日已用量未知，请核实申请')
            if db.execute("SELECT 1 FROM charges WHERE status='unknown'").fetchone(): raise BudgetPause('存在结果未知动作，需先对账')
            held=db.execute("SELECT COALESCE(SUM(estimate),0) FROM charges WHERE status='reserved'").fetchone()[0]
            if b['used']+held+estimate>self.LIMIT: raise BudgetPause('预计超过北京时间每日50分上限，请申请')
            db.execute('INSERT INTO charges VALUES(?,?,?,NULL,?)',(action,day,estimate,'reserved'))
    def unknown(self, action):
        with self.store.transaction() as db:
            if not db.execute("UPDATE charges SET status='unknown' WHERE action=? AND status='reserved'",(action,)).rowcount: raise BudgetPause('动作不可标记未知')
    def settle(self, action, actual):
        if not isinstance(actual,int) or actual<0: raise ValueError('实际用量未知，请保留unknown')
        with self.store.transaction() as db:
            r=db.execute('SELECT * FROM charges WHERE action=?',(action,)).fetchone()
            if not r: raise BudgetPause('动作不存在')
            if r['status']=='settled':
                if r['actual']!=actual: raise BudgetPause('对账冲突')
                return
            db.execute("UPDATE charges SET status='settled',actual=? WHERE action=?",(actual,action))
            db.execute('UPDATE budgets SET used=used+? WHERE day=?',(actual,r['day']))
            if actual>r['estimate']:
                db.execute('UPDATE budgets SET confirmed=0 WHERE day=?',(r['day'],))
