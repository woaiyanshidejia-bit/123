"""Local orchestration: explicit connections, durable paid pages, grounded review.

No calls occur at import or construction. Secrets live only in this process.
Production HTTP and CLI boundaries are injectable for offline integration tests.
"""
import json
import hashlib
from datetime import datetime, timezone
import threading
from pathlib import Path
from .adapters import ConnectionPause, MockSource, CodexSubscription
from .apollo import ApolloClient, build_search_request, SEARCH_PATH
from .budget import beijing_day
from .paid import PaidGateway, Observation
from .public_web import PublicVerifier
from .normalize import email

DEFAULT_FILTERS = {'q_organization_keyword_tags': ['lubricants']}

def settings(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('搜索设置必须为对象')
    filters = value.get('filters', DEFAULT_FILTERS)
    per_page = value.get('per_page', 100)
    request = build_search_request(1, per_page, filters)
    estimate = value.get('estimate_points')
    if estimate is not None and (type(estimate) is not int or not 1 <= estimate <= 50):
        raise ValueError('单页费用上界须为已核实的1至50分整数；未知请留空')
    label = value.get('account_label', '本机唯一Apollo账号')
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
        raise ValueError('请填写不含密钥的账号备注')
    return {'filters': {k: v for k, v in request.items() if k not in ('page', 'per_page')},
            'per_page': per_page, 'estimate_points': estimate, 'account_label': label.strip()}

class Runtime:
    def __init__(self, store, client_factory=ApolloClient, verifier=None, reviewer=None):
        self.store = store
        self.client_factory = client_factory
        self.verifier = verifier or PublicVerifier()
        self.reviewer = reviewer or CodexSubscription(receipt_path=Path(store.path).parent/'codex-acceptance.json')
        self._lock = threading.RLock()
        self._review_lock = threading.Lock()
        self._client = None
        self._authorized = False
        self._healthy = False
        self._account = None

    def save_settings(self, task, value):
        config = settings(value)
        with self.store.transaction() as db:
            if not db.execute('SELECT 1 FROM tasks WHERE id=?', (task,)).fetchone():
                raise ValueError('任务不存在')
            db.execute('INSERT INTO task_settings VALUES(?,?)', (task, json.dumps(config, ensure_ascii=False)))
        return config

    def connect(self, api_key, account_label, authorized=False):
        if authorized is not True:
            raise ConnectionPause('请明确授权本次运行使用已有Apollo权限；不会购买额度')
        label = settings({'account_label': account_label})['account_label']
        client = self.client_factory(api_key, enabled=True)
        result = client.health()  # Official no-credit health endpoint only.
        with self._lock:
            self._client = client
            self._authorized = True
            self._healthy = True
            self._account = label
        return {'healthy': True, 'reason': '认证健康检查通过；搜索权限、实际费用和完整采集尚待验收', 'health': result}

    def disconnect(self):
        with self._lock:
            self._client = None
            self._authorized = False
            self._healthy = False
            self._account = None

    def status(self):
        with self._lock:
            return {'implemented': True, 'health_checked': self._healthy,
                    'authorized_this_process': self._authorized, 'account_label': self._account,
                    'end_to_end_verified': False,
                    'reason': '仅本进程保存密钥；重启需重新连接。每页费用未知时保存响应并暂停对账。'}

    def source_for_task(self, task):
        if task['mode'] == 'simulation':
            return MockSource()
        rows = self.store.rows('SELECT config FROM task_settings WHERE task_id=?', (task['id'],))
        if not rows:
            raise ConnectionPause('旧任务缺少固定搜索设置；请新建真实任务，历史记录仍保留')
        return RealSource(self, task['id'], json.loads(rows[0]['config']))

    def validate_codex(self, authorized=False):
        if not self._review_lock.acquire(blocking=False):
            raise ConnectionPause('订阅审核或验收正在进行，请等待本次结束')
        try:
            return self.reviewer.validate_local(enabled=authorized is True)
        finally:
            self._review_lock.release()

    def preflight(self):
        if self._review_lock.locked():
            raise ConnectionPause('订阅验收正在进行，未执行付费搜索')
        with self._lock:
            if not self._client or not self._healthy or not self._authorized:
                raise ConnectionPause('Apollo 未连接；请在连接状态中进行官方免费健康检查')
        # Do not spend Apollo credits when the downstream review is unavailable.
        check = getattr(self.reviewer, 'preflight', None)
        if check:
            status = check()
            if not status.get('connected'):
                raise ConnectionPause(status.get('reason','Codex订阅能力尚未验收，未执行付费搜索'))
        else:
            status = self.reviewer.status()
            if not status.get('connected'):
                raise ConnectionPause('Codex ChatGPT订阅尚未登录或不可用，未启动付费搜索')
        return True

class RealSource:
    def __init__(self, runtime, task, config):
        self.runtime, self.task, self.config = runtime, task, config

    def search(self, page):
        request = build_search_request(page, self.config['per_page'], self.config['filters'])
        envelope = {'provider': 'apollo', 'endpoint': SEARCH_PATH,
                    'account_label': self.config['account_label'], 'query': request}
        action = f'apollo:{self.task}:page:{page}'
        # Cached reconciled responses work after restart with no API key or CLI.
        cached = self.runtime.store.rows('SELECT 1 FROM paid_results WHERE action=?', (action,))
        selected_client = None
        if not cached:
            self.runtime.preflight()
            with self.runtime._lock:
                selected_client = self.runtime._client
                if self.runtime._account != self.config['account_label']:
                    raise ConnectionPause('当前连接账号备注与任务不一致，已阻止付费操作')
            if self.config['estimate_points'] is None:
                raise ConnectionPause('本账号单页最大费用未知，请依据套餐核实后新建任务；未执行付费搜索')
        def transport(action_id, bound):
            # Each page requires a freshly confirmed account-wide baseline.
            # This process cannot observe deductions made by other applications.
            with self.runtime.store.transaction() as db:
                charge = db.execute('SELECT day FROM charges WHERE action=?',(action_id,)).fetchone()
                if not charge or charge['day'] != beijing_day():
                    raise ConnectionPause('预留后已跨北京时间日期；未发送，请核实该动作费用和今日预算')
                db.execute('UPDATE budgets SET confirmed=0 WHERE day=?', (charge['day'],))
            with self.runtime._lock:
                client = self.runtime._client
                if not client or client is not selected_client or not self.runtime._authorized or self.runtime._account != bound['account_label']:
                    raise ConnectionPause('连接已变更，未发送；未知动作须先对账')
                response = client.search(bound['query'], authorized=True)
            return Observation(response.to_payload())
        payload = PaidGateway(self.runtime.store, transport, verified=True).execute(
            action, self.config['estimate_points'], envelope)
        if not isinstance(payload, dict) or not isinstance(payload.get('records'), list) or type(payload.get('exhausted')) is not bool:
            raise ConnectionPause('已保存Apollo页结构无效，停止推进')
        return payload['records'], payload['exhausted']

    def verify(self, record):
        record_hash = hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        cached = self.runtime.store.rows('SELECT result FROM verification_cache WHERE task_id=? AND record_hash=?',(self.task,record_hash))
        if cached:
            result = json.loads(cached[0]['result'])
        else:
            result = self.runtime.verifier.verify(record)
            with self.runtime.store.transaction() as db:
                db.execute('INSERT OR IGNORE INTO verification_cache VALUES(?,?,?,?)',(self.task,record_hash,json.dumps(result,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        pages = result.get('evidence_pages', [])
        evidence = [{'id': str(i), 'url': p['url'], 'text': p['text']} for i, p in enumerate(pages)]
        if not self.runtime._review_lock.acquire(blocking=False):
            raise ConnectionPause('订阅验收正在进行，候选及网页证据已保存')
        try:
            review = self.runtime.reviewer.review_company(record, evidence, enabled=True)
        finally:
            self.runtime._review_lock.release()
        # Preserve the ordinary-code business-mailbox filter as a hard gate.
        allowed = {str(i): {email(a) for a in p['emails']} for i,p in enumerate(pages)}
        review['email_refs'] = [ref for ref in review['email_refs'] if email(ref['email']) in allowed.get(str(ref['evidence_id']),set())]
        # The bridge independently checks exact quotes, domains and email tokens.
        result.update(identity_confirmed=result['identity_confirmed'] is True and review['identity_confirmed'] is True,
                      business_confirmed=result['business_confirmed'] is True and review['business_confirmed'] is True, category=review['category'],
                      review_required=False, emails=[r['email'] for r in review['email_refs']])
        result['basis'] = 'Codex订阅结构化核实；引用经普通代码校验。' + review['reason']
        refs = review['identity_refs'] + review['business_refs'] + review['email_refs']
        for i, p in enumerate(pages):
            relevant = [r for r in refs if str(r['evidence_id']) == str(i)]
            p['basis'] = result['basis'] + ' 原文：' + ' | '.join(r['quote'] for r in relevant)
            p['emails'] = [r['email'] for r in review['email_refs'] if str(r['evidence_id']) == str(i)]
        return result
