"""Local orchestration: explicit connections, durable paid pages, grounded review.

No calls occur at import or construction. Secrets live only in this process.
Production HTTP and CLI boundaries are injectable for offline integration tests.
"""
import json
import hashlib
from datetime import datetime, timezone
import threading
import time
from pathlib import Path
from .adapters import ConnectionPause, MockSource, CodexSubscription
from .apollo import ApolloClient, build_search_request, build_accounts_request, SEARCH_PATH, ACCOUNTS_PATH, validate_bounded_search_payload, parse_accounts_response
from .budget import beijing_day
from .paid import PaidGateway, Observation, BoundedObservation
from .public_web import PublicVerifier
from .normalize import email

DEFAULT_FILTERS = {'q_organization_keyword_tags': ['lubricants']}

def settings(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('搜索设置必须为对象')
    source = value.get('source', 'apollo_organizations')
    if source not in ('apollo_organizations', 'apollo_accounts', 'apollo_import'):
        raise ValueError('不支持的候选来源')
    per_page = value.get('per_page', 100)
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError('每页公司数须为1至100的整数')
    filters = value.get('filters', {} if source != 'apollo_organizations' else DEFAULT_FILTERS)
    if source == 'apollo_organizations':
        request = build_search_request(1, per_page, filters)
    elif source == 'apollo_accounts':
        request = build_accounts_request(1, per_page, filters)
    else:
        if filters:
            raise ValueError('文件导入不能附加API搜索条件')
        request = {'page': 1, 'per_page': per_page}
    estimate = value.get('estimate_points')
    if source != 'apollo_organizations':
        estimate = None  # No estimate is used or substituted for an actual charge.
    elif estimate is not None and (type(estimate) is not int or not 1 <= estimate <= 50):
        raise ValueError('单页费用上界须为已核实的1至50分整数；未知请留空')
    label = value.get('account_label', '本机唯一Apollo账号')
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
        raise ValueError('请填写不含密钥的账号备注')
    result = {'source': source, 'filters': {k: v for k, v in request.items() if k not in ('page', 'per_page')},
              'per_page': per_page, 'estimate_points': estimate, 'account_label': label.strip()}
    if source == 'apollo_import':
        batch = value.get('batch_id')
        if not isinstance(batch, str) or len(batch) != 64 or any(c not in '0123456789abcdef' for c in batch):
            raise ValueError('请先通过Apollo候选导入入口保存文件')
        result['batch_id'] = batch
    if value.get('cost_policy_id') is not None:
        policy = value['cost_policy_id']
        if source != 'apollo_organizations' or not isinstance(policy, str) or not 1 <= len(policy) <= 100 or any(c not in '0123456789abcdef-' for c in policy):
            raise ValueError('费用授权标识无效')
        result['cost_policy_id'] = policy
    return result

class Runtime:
    def __init__(self, store, client_factory=ApolloClient, verifier=None, reviewer=None, cost_service=None):
        self.store = store
        self.client_factory = client_factory
        self.verifier = verifier or PublicVerifier()
        self.reviewer = reviewer or CodexSubscription(receipt_path=Path(store.path).parent/'codex-acceptance.json')
        from .cost_policy import CostPolicy
        self.costs = cost_service or CostPolicy(store, self.reviewer)
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
                    'reason': '已保存公司API明确0积分，不走付费对账；已有Apollo文件导入不需要API key。全库收费搜索仍受预算及对账限制。密钥仅本进程保存。'}

    def source_for_task(self, task):
        if task['mode'] == 'simulation':
            return MockSource()
        rows = self.store.rows('SELECT config FROM task_settings WHERE task_id=?', (task['id'],))
        if not rows:
            raise ConnectionPause('旧任务缺少固定搜索设置；请新建真实任务，历史记录仍保留')
        config = json.loads(rows[0]['config'])
        kind = config.get('source', 'apollo_organizations')
        source_class = {'apollo_organizations':RealSource, 'apollo_accounts':AccountsSource, 'apollo_import':ImportedSource}.get(kind)
        if source_class is None:
            raise ConnectionPause('任务来源无效，未启动任何接口')
        return source_class(self, task['id'], config)

    def validate_codex(self, authorized=False):
        if not self._review_lock.acquire(blocking=False):
            raise ConnectionPause('订阅审核或验收正在进行，请等待本次结束')
        try:
            return self.reviewer.validate_local(enabled=authorized is True)
        finally:
            self._review_lock.release()

    def assess_cost(self, config, account_evidence=None, authorized=False):
        self.preflight()
        if not self._review_lock.acquire(blocking=False):
            raise ConnectionPause('Codex正在核实其他内容，请稍后评估费用')
        try:
            with self._lock:
                if self._account != config['account_label']:
                    raise ConnectionPause('费用评估账号与当前连接不一致')
            return self.costs.assess(source=config.get('source','apollo_organizations'), account_label=config['account_label'],
                                     filters=config['filters'], per_page=config['per_page'],
                                     account_evidence=account_evidence, authorized=authorized)
        finally:
            self._review_lock.release()

    def bind_cost_policy(self, task, assessment):
        """Renew only the tariff authorization; immutable source filters remain."""
        from .engine import event
        with self.store.transaction() as db:
            t = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
            row = db.execute('SELECT config FROM task_settings WHERE task_id=?', (task,)).fetchone()
            if not t or not row or t['mode'] != 'real' or t['status'] not in ('paused','insufficient','complete') or (t['owner'] and t['lease'] > time.time()):
                raise ConnectionPause('请先暂停当前真实任务并等待在途请求结束，再续期收费授权')
            config = json.loads(row['config'])
            if config.get('source','apollo_organizations') != 'apollo_organizations':
                raise ConnectionPause('只有全库公司搜索任务需要收费授权')
            envelope = {'provider':'apollo','endpoint':SEARCH_PATH,'account_label':config['account_label'],
                        'query':build_search_request(1,config['per_page'],config['filters'])}
            policy = assessment.get('policy_id')
            PaidGateway(self.store)._policy(db,policy,envelope,assessment.get('max_points'),current=True)
            config.update(cost_policy_id=policy,estimate_points=assessment['max_points'])
            db.execute('UPDATE task_settings SET config=? WHERE task_id=?',(json.dumps(config,ensure_ascii=False),task))
            event(db,task,'cost_authorization','已绑定本日费用授权；请求条件保持固定，旧动作继续读取原响应')

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
        if self.config.get('cost_policy_id'):
            return self._bounded_search(action, envelope)
        # Cached reconciled responses work after restart with no API key or CLI.
        cached = self.runtime.store.rows('SELECT 1 FROM paid_results WHERE action=?', (action,))
        if not cached:
            self.runtime.preflight()
            raise ConnectionPause('此任务缺少当日Codex费用评估及代码授权，请暂停后在任务设置续期授权；旧已对账响应仍可恢复')
        payload = PaidGateway(self.runtime.store).execute(action, self.config['estimate_points'], envelope)
        if isinstance(payload, dict) and payload.get('response_state') == 'schema_review_required':
            raise ConnectionPause(payload.get('blocking_message','Apollo混合响应分页契约待核实；原始响应已保存，未推进'))
        if not isinstance(payload, dict) or not isinstance(payload.get('records'), list) or type(payload.get('exhausted')) is not bool:
            raise ConnectionPause('已保存Apollo页结构无效，停止推进')
        return payload['records'], payload['exhausted']

    def _bounded_search(self, action, envelope):
        prior = self.runtime.store.rows('SELECT a.policy_id,c.estimate FROM bounded_actions a JOIN charges c ON c.action=a.action WHERE a.action=?',(action,))
        policy = prior[0]['policy_id'] if prior else self.config['cost_policy_id']
        upper_bound = prior[0]['estimate'] if prior else self.config['estimate_points']
        selected_client = None
        if not prior:
            self.runtime.preflight()
            with self.runtime._lock:
                selected_client = self.runtime._client
                if self.runtime._account != self.config['account_label']:
                    raise ConnectionPause('当前账号与收费授权不一致')
        def transport(action_id, bound):
            charge = self.runtime.store.rows('SELECT day FROM charges WHERE action=?',(action_id,))
            if not charge or charge[0]['day'] != beijing_day():
                raise ConnectionPause('预留后跨日，未发送；需核清原动作')
            with self.runtime._lock:
                client = self.runtime._client
                if not client or client is not selected_client or not self.runtime._authorized or self.runtime._account != bound['account_label']:
                    raise ConnectionPause('连接已变化，未发送；原动作须核对')
                return BoundedObservation(client.search(bound['query'],authorized=True).to_payload())
        payload = PaidGateway(self.runtime.store,transport,verified=True).execute_bounded(
            action,upper_bound,envelope,authorization=policy,
            response_validator=lambda value:validate_bounded_search_payload(value,envelope['query']))
        if not validate_bounded_search_payload(payload,envelope['query']):
            raise ConnectionPause('缓存Apollo响应与请求不匹配，未推进')
        return payload['records'],payload['exhausted']

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


class AccountsSource(RealSource):
    """Documented zero-credit, read-only saved-account search; never enriches."""
    def search(self, page):
        request = build_accounts_request(page, self.config['per_page'], self.config['filters'])
        envelope = {'source':'apollo_accounts', 'endpoint':ACCOUNTS_PATH,
                    'account_label':self.config['account_label'], 'query':request}
        digest = PaidGateway.fingerprint(envelope)
        cached = self.runtime.store.rows('SELECT * FROM free_pages WHERE task_id=? AND logical_page=?',(self.task,page))
        if cached:
            row = cached[0]
            if row['request_hash'] != digest or hashlib.sha256(row['payload'].encode()).hexdigest() != row['payload_hash']:
                raise ConnectionPause('免费搜索页与固定请求或校验值不一致，停止推进')
            payload = json.loads(row['payload'])
        else:
            assessment = self.runtime.assess_cost(self.config)
            if assessment.get('classification') != 'free' or type(assessment.get('max_points')) is not int or assessment['max_points'] != 0:
                raise ConnectionPause('已保存公司读取费用尚未确认为免费，未发送请求')
            with self.runtime._lock:
                if not self.runtime._client or not self.runtime._authorized or self.runtime._account != self.config['account_label']:
                    raise ConnectionPause('Apollo连接或账号已变化，未读取已保存公司')
                payload = self.runtime._client.search_accounts(request).to_payload()
            # This bypass is for precisely this documented read-only endpoint,
            # never for a user-provided "free" flag or estimated price of zero.
            if payload.get('billing_status') != 'documented_free' or type(payload.get('actual_points')) is not int or payload['actual_points'] != 0 or payload.get('provenance') != 'saved_accounts':
                raise ConnectionPause('免费接口契约不匹配，未计为成功；不会切换收费搜索')
            serialized = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            with self.runtime.store.transaction() as db:
                db.execute('INSERT INTO free_pages VALUES(?,?,?,?,?,?)',(self.task,page,digest,serialized,hashlib.sha256(serialized.encode()).hexdigest(),datetime.now(timezone.utc).isoformat()))
        if not isinstance(payload.get('records'),list) or type(payload.get('exhausted')) is not bool:
            raise ConnectionPause('已保存公司的分页结构无效')
        # Reparse persisted pages too: a previous parser version or a modified
        # normalized row must not bypass today's pagination/identity contract.
        try:
            collected = payload['collected']
            timestamp = datetime.fromisoformat(collected)
            if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError()
            expected = parse_accounts_response(payload['raw'],request,provider_request_id=payload.get('provider_request_id')).to_payload()
            expected['collected'] = collected
            for record in expected['records']:
                record['collected'] = collected
            encode = lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            if encode(expected) != encode(payload):raise ValueError()
        except Exception:
            raise ConnectionPause('已保存免费响应未通过当前分页与原始记录复核，未推进且未重复请求') from None
        return payload['records'],payload['exhausted']


class ImportedSource(RealSource):
    """Local Apollo-export candidates; no Apollo client or paid gateway."""
    def search(self, page):
        from .apollo_import import page_import
        return page_import(self.runtime.store,self.config['batch_id'],page,self.config['per_page'])


def append_import(engine, task, batch_id):
    """Add an Apollo batch to a stopped, exhausted import task without API calls."""
    from .engine import event
    with engine.store.transaction() as db:
        t = db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        conf = db.execute('SELECT config FROM task_settings WHERE task_id=?',(task,)).fetchone()
        batch = db.execute('SELECT rows FROM apollo_imports WHERE batch_id=?',(batch_id,)).fetchone()
        if not t or not conf or not batch:
            raise ValueError('任务或导入批次不存在')
        config = json.loads(conf['config'])
        if config.get('source') != 'apollo_import' or t['mode'] != 'real':
            raise ValueError('仅可补充已有Apollo文件导入任务，不能改变API任务的固定来源')
        if (t['owner'] and t['lease'] > engine.clock()) or not t['exhausted']:
            raise ConnectionPause('请先完成原文件分页并暂停，等待当前核实结束后再补充导入')
        tag = 'apollo_import_batch:' + batch_id
        # The initial batch is checkpointed by the task's immutable settings;
        # it does not have the event emitted for later appended batches.
        # Check both forms before changing status or creating duplicate audit
        # rows. This also makes a retried append idempotent while newly-added
        # candidates are ready and have not yet been claimed by a worker.
        if batch_id == config.get('batch_id') or db.execute('SELECT 1 FROM events WHERE task=? AND kind=?',(task,tag)).fetchone():
            return {'id':task,'rows':0,'reason':'该候选批次已导入，未重复处理'}
        if t['status'] not in ('paused','insufficient','complete'):
            raise ConnectionPause('请先暂停任务，等待当前核实结束后再补充导入')
        for row in db.execute('SELECT record FROM apollo_import_rows WHERE batch_id=? ORDER BY row_number',(batch_id,)):
            engine._ingest(db,task,t['page'],json.loads(row['record']))
        db.execute("UPDATE tasks SET status='ready',reason='',owner=NULL,lease=0 WHERE id=?",(task,))
        event(db,task,tag,'用户声明Apollo候选补充导入；无Apollo调用；'+str(batch['rows'])+'行')
    return {'id':task,'rows':batch['rows'],'reason':'候选已补入原任务，继续官网核实；不会收费补搜'}
