"""Grounded, subscription-assisted Apollo cost classification.

Ordinary code owns the price ceiling and authorization. The reviewer supplies a
cited classification; it cannot create a fee, budget baseline, or permission.
Only public documentation is fetched here. Account text is user-declared.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import http.client
import ipaddress
import json
import re
import ssl
import socket
import threading
import time
from urllib.parse import urlsplit, urljoin, unquote
import uuid

from .adapters import ConnectionPause
from .apollo import SEARCH_PATH, ACCOUNTS_PATH, build_search_request, build_accounts_request
from .apollo_import import _source_url
from .budget import Budget, BudgetPause, beijing_day
from .paid import PaidGateway
from .public_web import PublicFetcher, PinnedHTTPS
from .codex_bridge import prepare_cost_evidence, validate_cost_review, ProtocolPause

ACCOUNT_DOC = 'https://docs.apollo.io/reference/search-for-accounts'
ORGANIZATION_DOC = 'https://docs.apollo.io/reference/organization-search'
PRICING_DOC = 'https://docs.apollo.io/docs/api-pricing'
OFFICIAL_DOCS = {ACCOUNT_DOC, ORGANIZATION_DOC, PRICING_DOC}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'template', 'svg'):
            self.skip += 1
        if not self.skip and tag in ('p', 'div', 'br', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'template', 'svg') and self.skip:
            self.skip -= 1
        if not self.skip and tag in ('p', 'div', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class OfficialDocumentReader:
    """Fixed public HTTPS documentation reader; no cookies, keys or retries."""
    MAX_BYTES = 2_000_000
    MAX_TEXT = 60_000

    def __init__(self, connection_factory=None, timeout=20, resolver=socket.getaddrinfo):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError('文档读取超时参数无效')
        self.connection_factory = connection_factory
        self.resolver = resolver
        self.timeout = timeout

    @staticmethod
    def _allowed(url):
        return url in OFFICIAL_DOCS or (url.endswith('.md') and url[:-3] in OFFICIAL_DOCS)

    def fetch(self, url):
        if url not in OFFICIAL_DOCS:
            raise ConnectionPause('仅允许读取固定Apollo官方费用文档')
        deadline = time.monotonic() + self.timeout
        current = url + '.md'
        redirects = 0
        while True:
            if not self._allowed(current) or time.monotonic() >= deadline:
                raise ConnectionPause('费用文档链接或总时限不符合要求，费用保持未知')
            parsed = urlsplit(current)
            remaining = max(.01, deadline - time.monotonic())
            if self.connection_factory is None:
                records = PublicFetcher(resolver=self.resolver).resolve('docs.apollo.io', 443, deadline)
                addresses = {item[4][0] for item in records}
                if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                    raise ConnectionPause('官方文档DNS不是公网地址，费用保持未知')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectionPause('官方文档DNS总时限已到')
                conn = PinnedHTTPS('docs.apollo.io', sorted(addresses)[0], 443, remaining / 3)
            else:
                # The injected test transport never changes the production host.
                conn = self.connection_factory('docs.apollo.io', timeout=remaining,
                                               context=ssl.create_default_context())
            expired = threading.Event()
            captured_socket = [None]
            def expire(current=conn, signal=expired, sockets=captured_socket):
                signal.set()
                sock = getattr(current, 'sock', None) or sockets[0]
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, ValueError):
                        pass
                try:
                    current.close()
                except OSError:
                    pass
            timer = threading.Timer(max(.01, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            try:
                conn.request('GET', parsed.path, headers={
                    'User-Agent': 'YINGBA-CostPolicy/1.0',
                    'Accept': 'text/markdown, text/html;q=0.9, text/plain;q=0.8',
                    'Accept-Encoding': 'identity', 'Connection': 'close'})
                captured_socket[0] = getattr(conn, 'sock', None)
                response = conn.getresponse()
                if expired.is_set() or time.monotonic() >= deadline:
                    raise ConnectionPause('费用文档总读取时限已到，保持未知')
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    redirects += 1
                    if not location or redirects > 2:
                        raise ConnectionPause('费用文档重定向异常，保持未知')
                    next_url = urljoin(current, location)
                    if not self._allowed(next_url) or next_url.removesuffix('.md') != url:
                        raise ConnectionPause('费用文档跳转超出固定官方页面，已拒绝')
                    current = next_url
                    continue
                if response.status == 404 and current == url + '.md':
                    current = url  # Official alternate representation only.
                    continue
                if response.status != 200:
                    raise ConnectionPause('官方费用文档暂不可读或拒绝访问，费用保持未知')
                if response.getheader('Content-Encoding', 'identity').casefold() != 'identity':
                    raise ConnectionPause('费用文档压缩格式未验收，保持未知')
                content_type = response.getheader('Content-Type', '').casefold()
                if not any(t in content_type for t in ('text/markdown', 'text/plain', 'text/html')):
                    raise ConnectionPause('费用文档格式无法核验，保持未知')
                chunks = []
                size = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise ConnectionPause('费用文档总读取时限已到，保持未知')
                    part = response.read1(min(8192, self.MAX_BYTES + 1 - size))
                    if expired.is_set() or time.monotonic() >= deadline:
                        raise ConnectionPause('费用文档总读取时限已到，保持未知')
                    if not part:
                        break
                    chunks.append(part)
                    size += len(part)
                    if size > self.MAX_BYTES:
                        raise ConnectionPause('费用文档超过安全大小限制，保持未知')
                body = b''.join(chunks)
                try:
                    text = body.decode('utf-8-sig', errors='strict')
                except UnicodeError:
                    raise ConnectionPause('费用文档不是可核验UTF-8文本，保持未知') from None
                if 'text/html' in content_type:
                    parser = _Text()
                    parser.feed(text)
                    text = ''.join(parser.parts)
                text = '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())
                if not text or len(text) > self.MAX_TEXT:
                    raise ConnectionPause('费用文档正文为空或过长，保持未知')
                return {'url': current, 'text': text, 'observed_at': _stamp(),
                        'content_sha256': hashlib.sha256(body).hexdigest()}
            except ConnectionPause:
                raise
            except (OSError, ValueError, http.client.HTTPException):
                raise ConnectionPause('官方费用文档连接失败，未改用非官方来源') from None
            finally:
                timer.cancel()
                conn.close()


def _official_rate(text, endpoint, amount):
    """Accept the endpoint's explicit fee row, never infer zero from absence."""
    plain = ' '.join(text.replace('`', '').replace('*', '').split())
    pos = plain.find('https://api.apollo.io' + endpoint)
    if pos < 0:
        return False
    segment = plain[pos:pos + 1800]
    matches = re.findall(r'Credit usage\s*\|?\s*(\d+)\s+credits?(?:\s+per\s+(page|record|request))?', segment, re.I)
    if len(matches) != 1 or int(matches[0][0]) != amount:
        return False
    if amount and matches[0][1].casefold() != 'page':
        return False
    extra = re.findall(r'This endpoint consumes\s+(\d+)\s+(?:Apollo\s+)?credits?\s+per\s+(page|record|request)', segment, re.I)
    return all(int(n) == amount and unit.casefold() == 'page' for n, unit in extra)


def _account_evidence(value, account_label):
    if not isinstance(value, dict):
        raise ConnectionPause('缺少当前账号套餐费用原文；未知费用不能授权')
    allowed = {'source_url', 'plan', 'text', 'confirmed_current_tariff', 'upper_bound',
               'no_external_spend', 'account_label'}
    if set(value) - allowed:
        raise ConnectionPause('账号费用声明含未支持字段，请仅提供套餐和费用依据，禁止附带凭据')
    if (value.get('confirmed_current_tariff') is not True or value.get('no_external_spend') is not True
            or type(value.get('upper_bound')) is not int or value['upper_bound'] != 1):
        raise ConnectionPause('须确认当前非旧套餐、每页1分上界及无账本外同时支出')
    url = value.get('source_url')
    try:
        url = _source_url(url)
    except ValueError:
        raise ConnectionPause('账号费用来源链接无效或含凭据，禁止保存') from None
    parsed = urlsplit(url)
    if (parsed.hostname != 'app.apollo.io' or len(url) > 2048 or parsed.query
            or re.search(r'api[_-]?key|token|password|authorization|credential|secret|session|code=', unquote(parsed.fragment), re.I)):
        raise ConnectionPause('账号费用原文须来自app.apollo.io的本人套餐页面')
    text, plan = value.get('text'), value.get('plan')
    if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 20_000
            or not isinstance(plan, str) or not 1 <= len(plan.strip()) <= 100):
        raise ConnectionPause('缺少可核对的当前套餐名称和账号页面原文')
    if (re.search(r'(?i)(?:api[ _-]?key|(?:access|refresh|auth)[ _-]?token|\btoken\b|authorization|password|credentials?|client[ _-]?secret|密钥|密码)\s*[\s:=：|]+\S+', text + '\n' + plan)
            or re.search(r'\bsk-[A-Za-z0-9_-]{16,}', text + '\n' + plan)):
        raise ConnectionPause('账号原文可能含敏感凭据，请删除密钥后再核实')
    plan = plan.strip()
    lower = text.casefold()
    if ('legacy' in plan.casefold() or '旧套餐' in plan or 'legacy plan' in lower
            or '旧套餐' in text or plan.casefold() not in lower):
        raise ConnectionPause('账号套餐未明确匹配当前费率或涉及旧套餐，不能确认上界')
    plain = ' '.join(text.split())
    if not re.search(r'(?:organization\s+search|mixed_companies/search|组织搜索|公司搜索).{0,180}?(?:(?<![\d.,])1\s*(?:apollo\s+)?(?:credits?|points?|积分|分)\s*(?:per\s+page|/\s*page|每页|/\s*页)|每页\s*1\s*(?:积分|分))', plain, re.I):
        raise ConnectionPause('账号原文缺少本次公司搜索按页1分的明确费用依据')
    rates = re.findall(r'(\d+(?:\.\d+)?)\s*(?:apollo\s+)?(?:credits?|points?|积分|分)\s*(?:per\s+page|/\s*page|每页|/\s*页)', plain, re.I)
    rates += re.findall(r'每页\s*(\d+(?:\.\d+)?)\s*(?:积分|分)', plain)
    if not rates or any(rate != '1' for rate in rates):
        raise ConnectionPause('账号原文包含冲突或不明确的按页费用，保持未知')
    if value.get('account_label', account_label) != account_label:
        raise ConnectionPause('账号费用声明与当前账号备注不一致')
    return {'id': 'account', 'url': url, 'text': text, 'kind': 'account_page',
            'observed_at': _stamp(), 'plan': plan, 'upper_bound': 1,
            'confirmed_current_tariff': True, 'no_external_spend': True,
            'attestation': '用户声明为当前账号页面原文；程序未在线认证账号套餐'}


class CostPolicy:
    def __init__(self, store, reviewer, document_reader=None):
        self.store = store
        self.reviewer = reviewer
        self.reader = document_reader or OfficialDocumentReader()
        self._lock = threading.RLock()

    def _read(self, url, evidence_id):
        method = self.reader.fetch if hasattr(self.reader, 'fetch') else self.reader
        doc = method(url)
        if isinstance(doc, str):
            doc = {'url': url, 'text': doc, 'observed_at': _stamp()}
        if (not isinstance(doc, dict) or not isinstance(doc.get('url'), str) or doc['url'].removesuffix('.md') != url
                or not isinstance(doc.get('text'), str) or not 1 <= len(doc['text']) <= 60_000):
            raise ConnectionPause('官方费用文档返回内容无法核验，保持未知')
        return {'id': evidence_id, 'url': doc['url'], 'text': doc['text'],
                'kind': 'official_docs', 'observed_at': doc.get('observed_at', _stamp()),
                'content_sha256': doc.get('content_sha256', hashlib.sha256(doc['text'].encode()).hexdigest())}

    def _save(self, assessment_id, day, source, endpoint, account, request_hash,
              classification, amount, reason, evidence, review, policy_id=None):
        with self.store.transaction() as db:
            db.execute('''INSERT INTO cost_assessments(assessment_id,day,source,endpoint,account_label,
                request_hash,classification,max_points,reason,evidence,model_review,policy_id,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (assessment_id, day, source, endpoint, account, request_hash, classification, amount,
                 reason, _json(evidence), _json(review), policy_id, _stamp()))

    @staticmethod
    def _checked_review(review, endpoint, amount, plan, evidence):
        try:
            payload = prepare_cost_evidence({'endpoint': endpoint, 'unit': 'page', 'applicable_plan': plan}, evidence)
            validate_cost_review(review, payload)
        except ProtocolPause as exc:
            raise ConnectionPause(str(exc)) from None
        expected = 'free' if amount == 0 else 'paid'
        if (not isinstance(review, dict) or review.get('classification') != expected
                or review.get('endpoint') != endpoint or review.get('unit') != 'page'
                or type(review.get('max_points')) is not int or review['max_points'] != amount
                or (amount and review.get('applicable_plan') != plan)):
            raise ConnectionPause('Codex费用结论与代码核验的端点、单位、套餐或上界不一致')
        by_id = {item['id']: item for item in evidence}
        refs = review.get('refs')
        if not isinstance(refs, list) or not refs:
            raise ConnectionPause('费用判断缺少可核对引用，保持未知')
        ids = set()
        for ref in refs:
            if not isinstance(ref, dict) or ref.get('evidence_id') not in by_id:
                raise ConnectionPause('费用引用不属于当前证据')
            quote = ref.get('quote')
            if not isinstance(quote, str) or not quote.strip() or quote not in by_id[ref['evidence_id']]['text']:
                raise ConnectionPause('费用引用没有逐字出现在当前证据中')
            ids.add(ref['evidence_id'])
        if 'endpoint' not in ids or (amount and 'account' not in ids):
            raise ConnectionPause('费用判断须同时有官方端点与适用账号套餐的原文依据')
        endpoint_quotes = ' '.join(ref['quote'] for ref in refs if ref['evidence_id'] == 'endpoint')
        rate_pattern = r'(?<![\w.,])' + str(amount) + r'\s*(?:Apollo\s+)?credits?\b'
        if not re.search(rate_pattern, endpoint_quotes, re.I) or (amount and not re.search(r'per\s+page', endpoint_quotes, re.I)):
            raise ConnectionPause('官方费用引用没有明确包含本次按页费率')
        if amount:
            account_quotes = ' '.join(ref['quote'] for ref in refs if ref['evidence_id'] == 'account')
            if plan.casefold() not in account_quotes.casefold() or not re.search(r'(?<![\w.,])1\s*(?:Apollo\s+)?(?:credits?\b|points?\b|积分|分)', account_quotes, re.I):
                raise ConnectionPause('账号引用未同时包含适用套餐及1分费用')
        for key in ('effective_at', 'valid_until'):
            value = review.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not any(value in ref['quote'] for ref in refs):
                raise ConnectionPause('费用生效日期不是引用原文中的日期')
            try:
                if len(value) == 10:
                    before, after = value < beijing_day(), value > beijing_day()
                else:
                    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    before, after = moment < now, moment > now
                if (key == 'effective_at' and after) or (key == 'valid_until' and before):
                    raise ConnectionPause('费用规则尚未生效或已经过期')
            except ValueError:
                raise ConnectionPause('费用规则日期无法核验') from None

    def assess(self, source, account_label, filters, per_page, account_evidence=None, authorized=False):
        if source not in ('apollo_accounts', 'apollo_organizations'):
            raise ConnectionPause('仅评估已实现的Apollo端点；文件导入没有Apollo调用费用')
        if (not isinstance(account_label, str) or not 1 <= len(account_label.strip()) <= 100 or any(ord(c) < 32 for c in account_label)
                or re.search(r'api.?key|token|password|authorization|credential|secret|\bsk-', account_label, re.I)):
            raise ConnectionPause('费用评估需要有效账号备注')
        try:
            request = (build_accounts_request if source == 'apollo_accounts' else build_search_request)(1, per_page, filters)
        except (ValueError, TypeError):
            raise ConnectionPause('费用评估的搜索条件无效') from None
        endpoint = ACCOUNTS_PATH if source == 'apollo_accounts' else SEARCH_PATH
        envelope = {'provider': 'apollo', 'endpoint': endpoint, 'account_label': account_label, 'query': request}
        day = beijing_day()
        assessment_id = uuid.uuid4().hex
        evidence = []
        review = {}
        amount = 0 if source == 'apollo_accounts' else 1
        # Invalid account text must never reach model or disk, even as an error record.
        account = _account_evidence(account_evidence, account_label) if amount else None
        request_hash = _hash({'request': envelope, 'account_evidence': account_evidence if amount else None})
        with self._lock:
            cached = self.store.rows('''SELECT * FROM cost_assessments WHERE day=? AND source=? AND endpoint=?
                AND account_label=? AND request_hash=? AND classification=? ORDER BY created DESC LIMIT 1''',
                (day, source, endpoint, account_label, request_hash, 'paid' if amount else 'free'))
            try:
                if cached:
                    row = cached[0]
                    evidence, review = json.loads(row['evidence']), json.loads(row['model_review'])
                    self._checked_review(review, endpoint, amount, account['plan'] if account else None, evidence)
                    assessment_id = row['assessment_id']
                else:
                    endpoint_doc = self._read(ACCOUNT_DOC if not amount else ORGANIZATION_DOC, 'endpoint')
                    evidence.append(endpoint_doc)
                    if not _official_rate(endpoint_doc['text'], endpoint, amount):
                        raise ConnectionPause('官方端点费用条款未明确匹配，未知不能当作零费用')
                    if amount:
                        pricing = self._read(PRICING_DOC, 'pricing')
                        evidence.append(pricing)
                        plain = ' '.join(pricing['text'].replace('`', '').replace('*', '').split())
                        if (not re.search(r'organization search.{0,120}?(?<![\d.,])1 credits? per page', plain, re.I)
                                or not re.search(r'legacy plan', plain, re.I)):
                            raise ConnectionPause('官方通用价格表或旧套餐适用范围未核清，不能授权')
                        evidence.append(account)
                    action = {'provider': 'apollo', 'method': 'POST', 'endpoint': endpoint, 'unit': 'page',
                              'applicable_plan': account['plan'] if account else None, 'quantity': 1, 'page_size': per_page}
                    review = self.reviewer.review_cost(action, evidence, enabled=True)
                    self._checked_review(review, endpoint, amount, account['plan'] if account else None, evidence)
                    if beijing_day() != day:
                        raise ConnectionPause('费用评估跨越北京时间日期，请重新核实今日费用')
                    self._save(assessment_id, day, source, endpoint, account_label, request_hash,
                               'paid' if amount else 'free', amount, review.get('reason', ''), evidence, review)
                # An assessment cache is evidence, never an authorization token.
                # Prior policies may have expired or been explicitly revoked.
                policy_id = None
                if beijing_day() != day:
                    raise ConnectionPause('费用评估跨越北京时间日期，请重新核实今日费用')
                if amount and authorized is True:
                    baseline = self.store.rows('SELECT used,confirmed FROM budgets WHERE day=?', (day,))
                    if baseline and baseline[0]['confirmed'] and baseline[0]['used'] + 1 > Budget.LIMIT:
                        raise ConnectionPause('预计超过北京时间每日50分上限，请申请')
                    authorization_evidence = {'assessment_id': assessment_id, 'documents': evidence,
                                              'review': review, 'account_declaration': account_evidence,
                                              'authorized': True, 'request': envelope}
                    # Each explicit authorization creates an immutable policy. A
                    # cached assessment never silently restores a revoked policy.
                    policy_id = PaidGateway(self.store).authorize_bounded(
                        uuid.uuid4().hex, account_label, envelope, 1, authorization_evidence)
                    with self.store.transaction() as db:
                        db.execute('UPDATE cost_assessments SET policy_id=? WHERE assessment_id=?', (policy_id, assessment_id))
                return {'assessment_id': assessment_id, 'classification': 'paid' if amount else 'free',
                        'policy_id': policy_id, 'max_points': amount, 'reason': review.get('reason', ''),
                        'evidence': evidence, 'cached': bool(cached), 'account_provenance':
                        account['attestation'] if account else '官方公开端点文档；不代表账号访问权限'}
            except (ConnectionPause, BudgetPause) as exc:
                if not cached:
                    # A price can be known while budget authorization fails. Keep
                    # its prior assessment; never relabel it as authorized success.
                    exists = self.store.rows('SELECT 1 FROM cost_assessments WHERE assessment_id=?', (assessment_id,))
                    if not exists:
                        self._save(assessment_id, day, source, endpoint, account_label, request_hash,
                                   'unknown', None, str(exc), evidence, review)
                raise ConnectionPause(str(exc)) from None
