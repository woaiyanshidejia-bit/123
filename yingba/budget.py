"""Fail-closed shared account ledger; SQLite serializes all local task reservations.

The confirmed total must cover the entire provider account. This database cannot
observe charges made by another installation or outside this application.
"""
from datetime import datetime, timezone, timedelta, date


class BudgetPause(Exception):
    pass


def utc_now(now=None):
    value = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('时间必须包含时区，禁止依赖电脑本地时区')
    return value.astimezone(timezone.utc)


def beijing_day(now=None):
    return utc_now(now).astimezone(timezone(timedelta(hours=8))).date().isoformat()


def validate_action(action):
    if not isinstance(action, str) or not action.strip() or len(action) > 512 or any(ord(c) < 32 for c in action):
        raise BudgetPause('动作ID必须为有效且唯一的字符串')


def canonical_day(value):
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise BudgetPause('实际扣费日期无效') from None
    if parsed.isoformat() != value:
        raise BudgetPause('实际扣费日期必须为 YYYY-MM-DD')
    return value


class Budget:
    LIMIT = 50
    MAX_LEDGER_POINTS = (1 << 63) - 1

    def __init__(self, store):
        self.store = store

    def confirm(self, used, now=None, basis='manual-account-total'):
        if type(used) is not int or not 0 <= used <= self.MAX_LEDGER_POINTS:
            raise ValueError('已用量必须为非负整数')
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError('账户已用量需要核实依据')
        moment = utc_now(now)
        day = beijing_day(moment)
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM charges WHERE status IN ('reserved','unknown','observed_pending')").fetchone():
                raise BudgetPause('先对账所有未结算动作，禁止覆盖账户基线')
            prior = db.execute('SELECT used FROM budgets WHERE day=?', (day,)).fetchone()
            if prior and used < prior['used']:
                raise BudgetPause('已核实用量不得低于账本已记用量，需人工复核')
            db.execute('INSERT INTO budgets VALUES(?,?,1) ON CONFLICT(day) DO UPDATE SET used=excluded.used,confirmed=1', (day, used))
            db.execute('INSERT INTO budget_confirmations(day,used,confirmed_at,basis) VALUES(?,?,?,?)', (day, used, moment.isoformat(), basis))

    def reserve(self, action, estimate, now=None):
        with self.store.transaction() as db:
            self._reserve(db, action, estimate, now)

    def _reserve(self, db, action, estimate, now=None):
        """Internal transaction seam: bind request and reservation in one commit."""
        validate_action(action)
        if type(estimate) is not int or estimate < 0:
            raise BudgetPause('预计用量未知')
        day = beijing_day(now)
        if db.execute('SELECT 1 FROM charges WHERE action=?', (action,)).fetchone():
            raise BudgetPause('相同动作已登记，先查询或对账，禁止重复执行')
        baseline = db.execute('SELECT * FROM budgets WHERE day=?', (day,)).fetchone()
        if not baseline or not baseline['confirmed']:
            raise BudgetPause('全账号今日已用量未知，请核实申请')
        if db.execute("SELECT 1 FROM charges WHERE status='unknown'").fetchone():
            raise BudgetPause('存在结果未知动作，需先对账')
        if db.execute("SELECT 1 FROM charges WHERE status='observed_pending' AND day<>?", (day,)).fetchone():
            raise BudgetPause('存在跨日待结清的已验证响应，先核实实际费用和扣费日')
        # Include old-day reservations too: their charge day is not yet known.
        held = db.execute("SELECT COALESCE(SUM(estimate),0) FROM charges WHERE status IN ('reserved','observed_pending')").fetchone()[0]
        if baseline['used'] + held + estimate > self.LIMIT:
            raise BudgetPause('预计超过北京时间每日50分上限，请申请')
        db.execute('INSERT INTO charges VALUES(?,?,?,NULL,?)', (action, day, estimate, 'reserved'))

    def unknown(self, action):
        validate_action(action)
        with self.store.transaction() as db:
            if not db.execute("UPDATE charges SET status='unknown' WHERE action=? AND status='reserved'", (action,)).rowcount:
                raise BudgetPause('动作不可标记未知')

    def settle(self, action, actual, billed_day=None, now=None):
        with self.store.transaction() as db:
            self._settle(db, action, actual, billed_day, now)

    def _settle(self, db, action, actual, billed_day=None, now=None):
        validate_action(action)
        if type(actual) is not int or not 0 <= actual <= self.MAX_LEDGER_POINTS:
            raise ValueError('实际用量未知，请保留unknown')
        if billed_day is not None:
            canonical_day(billed_day)
        row = db.execute('SELECT * FROM charges WHERE action=?', (action,)).fetchone()
        if not row:
            raise BudgetPause('动作不存在')
        if row['status'] == 'settled':
            if row['actual'] != actual:
                raise BudgetPause('对账冲突')
            known = db.execute('SELECT billed_day FROM charge_settlements WHERE action=?', (action,)).fetchone()
            if not known or (billed_day is not None and known['billed_day'] != billed_day):
                raise BudgetPause('已结算扣费归属日冲突或账本不完整')
            return
        if row['status'] not in ('reserved', 'unknown', 'observed_pending'):
            raise BudgetPause('付费账本状态异常，需人工复核')
        today = beijing_day(now)
        if actual > 0 and row['day'] != today and billed_day is None:
            raise BudgetPause('跨日结算必须核实服务商实际扣费归属日，保持冻结')
        settled_day = billed_day or row['day']
        canonical_day(settled_day)
        if not row['day'] <= settled_day <= today:
            raise BudgetPause('扣费归属日不在动作发生至今日范围')
        baseline = db.execute('SELECT used FROM budgets WHERE day=?', (settled_day,)).fetchone()
        if baseline and baseline['used'] + actual > self.MAX_LEDGER_POINTS:
            raise BudgetPause('账本用量超出整数存储范围，请人工核对，保持冻结')
        db.execute("UPDATE charges SET status='settled',actual=? WHERE action=?", (actual, action))
        db.execute('INSERT INTO charge_settlements VALUES(?,?)', (action, settled_day))
        db.execute('INSERT OR IGNORE INTO budgets VALUES(?,0,0)', (settled_day,))
        db.execute('UPDATE budgets SET used=used+? WHERE day=?', (actual, settled_day))
        if actual > row['estimate']:
            # A violated estimate invalidates the account assumption for any day
            # where another confirmed baseline might otherwise allow dispatch.
            db.execute('UPDATE budgets SET confirmed=0')
            # An observed charge above its verified ceiling invalidates that
            # account's batch authorization. A new baseline alone cannot restore it.
            db.execute('''UPDATE paid_policies SET confirmed=0 WHERE account_label IN (
                SELECT p.account_label FROM paid_policies p JOIN bounded_actions a
                ON a.policy_id=p.id WHERE a.action=?)''', (action,))
