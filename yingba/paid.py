"""Crash-safe transport boundary. Credentials must never be part of request/payload.

A response is not proof of billing. Transports without a reliable per-action
charge receipt return Observation; the response is saved and execution pauses
until manual reconciliation. No path here retries a network request.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from .budget import Budget, BudgetPause, validate_action, beijing_day, canonical_day
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


@dataclass(frozen=True)
class BoundedObservation:
    """Response requiring independent schema validation and a current policy.

    This marker alone grants no authorization and is never a billing receipt.
    """
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
    def _template(request, variable_page):
        snapshot = json.loads(canonical_json(request))
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('account_label'), str) or not snapshot['account_label'].strip():
            raise BudgetPause('收费授权请求必须绑定账号备注')
        if variable_page:
            query = snapshot.get('query')
            if not isinstance(query, dict) or type(query.get('page')) is not int or not 1 <= query['page'] <= 500:
                raise BudgetPause('收费批次只允许已固定搜索条件下的1至500页')
            query['page'] = 1
        return canonical_json(snapshot)

    @staticmethod
    def _policy_digest(policy):
        return digest(canonical_json({key: policy[key] for key in (
            'id', 'account_label', 'day', 'template_hash', 'upper_bound',
            'variable_page', 'evidence_hash')}))

    def authorize_bounded(self, policy_id, account_label, request_template,
                          upper_bound, evidence, *, variable_page=True):
        """Trusted code boundary, NOT a direct UI/API authorization endpoint.

        The caller must independently validate current account-plan evidence,
        the price ceiling, and the user's account-wide spending authorization.
        Model output or the user's estimate alone must never call this method.
        """
        validate_action(policy_id)
        if type(upper_bound) is not int or not 0 <= upper_bound <= Budget.LIMIT:
            raise BudgetPause('收费授权需要代码核实的0至50分整数上界')
        if type(variable_page) is not bool or not isinstance(evidence, dict) or not evidence:
            raise BudgetPause('缺少可核验的套餐与授权依据')
        template = self._template(request_template, variable_page)
        if json.loads(template)['account_label'] != account_label:
            raise BudgetPause('收费授权账号与请求模板不符')
        serialized_evidence = canonical_json(evidence)
        policy = {'id': policy_id, 'account_label': account_label, 'day': beijing_day(),
                  'request_template': template, 'template_hash': digest(template),
                  'upper_bound': upper_bound, 'variable_page': int(variable_page),
                  'evidence': serialized_evidence, 'evidence_hash': digest(serialized_evidence)}
        policy['policy_hash'] = self._policy_digest(policy)
        with self.store.transaction() as db:
            baseline = db.execute('SELECT confirmed FROM budgets WHERE day=?', (policy['day'],)).fetchone()
            if not baseline or not baseline['confirmed']:
                raise BudgetPause('全账号今日用量未核实，不能批准收费批次')
            if db.execute("SELECT 1 FROM charges WHERE status IN ('reserved','unknown') OR (status='observed_pending' AND day<>?)", (policy['day'],)).fetchone():
                raise BudgetPause('存在未明确或跨日未结清动作，不能批准收费批次')
            prior = db.execute('SELECT * FROM paid_policies WHERE id=?', (policy_id,)).fetchone()
            if prior:
                if prior['policy_hash'] != policy['policy_hash'] or not prior['confirmed']:
                    raise BudgetPause('收费授权已变更或撤销，请重新核实；禁止覆盖原授权')
                self._policy(db, policy_id, request_template, upper_bound, current=True)
                return policy_id
            if db.execute("SELECT 1 FROM charges WHERE status IN ('reserved','unknown','observed_pending')").fetchone():
                raise BudgetPause('存在未结清动作，不能变更或新建收费批次授权')
            db.execute('''INSERT INTO paid_policies(id,account_label,day,request_template,template_hash,
                upper_bound,variable_page,evidence,evidence_hash,policy_hash,confirmed,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,1,?)''', tuple(policy[key] for key in (
                    'id','account_label','day','request_template','template_hash','upper_bound',
                    'variable_page','evidence','evidence_hash','policy_hash')) + (datetime.now(timezone.utc).isoformat(),))
        return policy_id

    def revoke_bounded(self, policy_id):
        validate_action(policy_id)
        with self.store.transaction() as db:
            db.execute('UPDATE paid_policies SET confirmed=0 WHERE id=?', (policy_id,))

    def _policy(self, db, policy_id, request, upper_bound, *, current):
        row = db.execute('SELECT * FROM paid_policies WHERE id=?', (policy_id,)).fetchone()
        if not row or type(upper_bound) is not int or row['upper_bound'] != upper_bound:
            raise BudgetPause('缺少匹配的代码收费授权，禁止按估算执行')
        if (digest(row['request_template']) != row['template_hash'] or digest(row['evidence']) != row['evidence_hash']
                or self._policy_digest(row) != row['policy_hash']
                or self._template(request, bool(row['variable_page'])) != row['request_template']
                or request['account_label'] != row['account_label']):
            raise BudgetPause('收费授权的请求、账号或依据校验失败')
        if current and (row['day'] != beijing_day() or not row['confirmed']):
            raise BudgetPause('收费授权已过期或撤销，需核实今日套餐和全账号额度')
        return row

    def _bounded_binding(self, db, action, request, upper_bound, authorization):
        policy = self._policy(db, authorization, request, upper_bound, current=False)
        bound = db.execute('''SELECT a.*,c.day,c.estimate,c.status,c.actual FROM bounded_actions a
            JOIN charges c ON c.action=a.action WHERE a.action=?''', (action,)).fetchone()
        if (not bound or bound['policy_id'] != authorization or bound['policy_hash'] != policy['policy_hash']
                or bound['day'] != policy['day'] or bound['estimate'] != upper_bound):
            raise BudgetPause('原动作与收费授权不匹配，禁止重放或重发')
        self._binding(db, action, self.fingerprint(request))
        return bound

    @staticmethod
    def _validate_bounded_response(payload, validator):
        if (not isinstance(payload, dict) or not isinstance(payload.get('records'), list)
                or type(payload.get('exhausted')) is not bool
                or payload.get('schema_review_required') not in (None, False)
                or payload.get('response_state') == 'schema_review_required'
                or payload.get('actual_points') is not None):
            raise BudgetPause('响应不完整或计费声明未知，禁止提升为已验证响应')
        if not callable(validator):
            raise BudgetPause('已核实费用不等于响应已验收；需要可信响应校验，禁止重发')
        try:
            valid = validator(json.loads(canonical_json(payload))) is True
        except Exception:
            valid = False
        if not valid:
            raise BudgetPause('收费响应结构或归属未知，保持暂停')

    def execute_bounded(self, action, upper_bound, request, *, authorization,
                        response_validator):
        """Return verified responses while retaining the full ceiling as held.

        Actual cost remains NULL until receipt reconciliation. Unknown HTTP or
        schema outcomes globally freeze dispatch. The validator must be trusted
        deterministic integration code; never a model-generated predicate.
        """
        validate_action(action)
        validate_action(authorization)
        serialized_request = canonical_json(request)
        request = json.loads(serialized_request)
        fingerprint = digest(serialized_request)
        with self.store.transaction() as db:
            charge = db.execute('SELECT status FROM charges WHERE action=?', (action,)).fetchone()
            if charge:
                bound = self._bounded_binding(db, action, request, upper_bound, authorization)
                cached = self._cached(db, action, fingerprint)
                if cached:
                    if not bound['observed_at']:
                        # Financial reconciliation cannot approve an unverified
                        # business response. Validate cached bytes, never refetch.
                        self._validate_bounded_response(json.loads(cached['payload']), response_validator)
                        db.execute('UPDATE bounded_actions SET observed_at=? WHERE action=?', (datetime.now(timezone.utc).isoformat(), action))
                    return json.loads(cached['payload'])
                if bound['status'] == 'observed_pending':
                    observation = db.execute('SELECT * FROM paid_observations WHERE action=?', (action,)).fetchone()
                    if not observation or not bound['observed_at'] or bound['actual'] is not None or digest(observation['payload']) != observation['response_hash']:
                        raise BudgetPause('已验证待结算响应校验失败，禁止重发')
                    return json.loads(observation['payload'])
                raise BudgetPause('原收费动作结果仍未知，必须对账且禁止重新请求')
            if not self.verified or self.transport is None or not callable(response_validator):
                raise ConnectionPause('收费接口或确定性响应校验未连接，未预留或执行')
            policy = self._policy(db, authorization, request, upper_bound, current=True)
            self.budget._reserve(db, action, upper_bound)
            db.execute('INSERT INTO paid_requests VALUES(?,?,?,?)', (action, fingerprint, serialized_request, datetime.now(timezone.utc).isoformat()))
            db.execute('INSERT INTO bounded_actions VALUES(?,?,?,NULL)', (action, authorization, policy['policy_hash']))
            db.execute("UPDATE charges SET status='unknown' WHERE action=?", (action,))
        try:
            observation = self.transport(action, json.loads(serialized_request))
            if isinstance(observation, (BoundedObservation, Observation, Receipt)):
                self.capture(action, request, observation.payload, observation.external_reference)
            if not isinstance(observation, BoundedObservation):
                raise BudgetPause('收到的响应不具备收费批次验证条件')
            # Give the validator a separate JSON snapshot so it cannot mutate
            # the response that will be persisted/replayed.
            self._validate_bounded_response(observation.payload, response_validator)
            with self.store.transaction() as db:
                bound = self._bounded_binding(db, action, request, upper_bound, authorization)
                self._policy(db, authorization, request, upper_bound, current=True)
                saved = db.execute('SELECT * FROM paid_observations WHERE action=?', (action,)).fetchone()
                if bound['status'] != 'unknown' or not saved or digest(saved['payload']) != saved['response_hash'] or saved['response_hash'] != digest(canonical_json(observation.payload)):
                    raise BudgetPause('响应保存或动作状态校验失败，保持冻结')
                db.execute("UPDATE charges SET status='observed_pending' WHERE action=?", (action,))
                db.execute('UPDATE bounded_actions SET observed_at=? WHERE action=?', (datetime.now(timezone.utc).isoformat(), action))
            return json.loads(saved['payload'])
        except Exception:
            raise UnknownOutcome('收费动作结果或响应验证未明确，已保存可保存内容并冻结；禁止盲重试') from None

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
                bounded = db.execute('SELECT observed_at FROM bounded_actions WHERE action=?', (action,)).fetchone()
                if bounded:
                    raise BudgetPause('此动作属于收费批次，须通过原授权及响应校验入口读取')
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
            if not charge or charge['status'] not in ('unknown', 'settled', 'observed_pending'):
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
            o.collected AS response_collected,o.external_reference,a.policy_id,
            CASE WHEN c.status='observed_pending' THEN 1 ELSE 0 END AS response_verified
            FROM charges c LEFT JOIN paid_requests r ON r.action=c.action
            LEFT JOIN paid_observations o ON o.action=c.action
            LEFT JOIN bounded_actions a ON a.action=c.action
            WHERE c.status IN ('reserved','unknown','observed_pending') ORDER BY c.day,c.action''')

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
        with self.store.transaction() as db:
            return self._reconcile(db, action, request, receipt)

    def _reconcile(self, db, action, request, receipt):
        validate_action(action)
        if not isinstance(receipt, Receipt):
            raise ValueError('缺可靠回执；仅收到响应时请保存 Observation')
        valid_reference(receipt.external_reference)
        serialized = canonical_json(receipt.payload)
        fingerprint = self.fingerprint(request)
        if receipt.request_hash is not None and receipt.request_hash != fingerprint:
            raise BudgetPause('回执请求指纹不匹配')
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

    def reconcile_verified_batch(self, actions, actual_each, billed_day, reference,
                                 *, confirmed_each=False):
        """Settle explicitly reviewed equal actual costs, all-or-nothing.

        The caller must confirm each selected action's real charge from account
        evidence. Neither a tariff nor a ceiling is an actual charge. Unknown
        results, mixed policies/days and unverified responses are ineligible.
        """
        if confirmed_each is not True:
            raise BudgetPause('请先逐项核实所选动作实扣相同；标价或费用上界不能代替实扣')
        if not isinstance(actions, list) or not 1 <= len(actions) <= 500:
            raise ValueError('请明确选择1至500个待结算动作')
        for action in actions:
            validate_action(action)
        if len(set(actions)) != len(actions):
            raise BudgetPause('批次动作重复，未执行任何结算')
        if type(actual_each) is not int or not 0 <= actual_each <= Budget.MAX_LEDGER_POINTS:
            raise ValueError('逐项实际用量必须为已核实的非负整数')
        canonical_day(billed_day)
        valid_reference(reference)
        actions = sorted(actions)
        prepared = []
        policy_id = action_day = None
        with self.store.transaction() as db:
            for action in actions:
                row = db.execute('''SELECT c.day,c.estimate,c.status,c.actual,r.request_json,
                    r.request_hash,o.payload,o.response_hash,o.external_reference,a.policy_id,a.observed_at
                    FROM charges c JOIN paid_requests r ON r.action=c.action
                    JOIN paid_observations o ON o.action=c.action JOIN bounded_actions a ON a.action=c.action
                    WHERE c.action=?''', (action,)).fetchone()
                if not row or row['status'] != 'observed_pending' or row['actual'] is not None or not row['observed_at']:
                    raise BudgetPause('所选动作包含未知、未验证或已结算结果，整批未修改')
                if policy_id is None:
                    policy_id, action_day = row['policy_id'], row['day']
                elif row['policy_id'] != policy_id or row['day'] != action_day:
                    raise BudgetPause('批次必须属于同一收费授权及动作发生日，整批未修改')
                if row['request_json'] is None or digest(row['payload']) != row['response_hash']:
                    raise BudgetPause('批次请求或响应校验失败，整批未修改')
                request = json.loads(row['request_json'])
                self._bounded_binding(db, action, request, row['estimate'], policy_id)
                # Keep an already captured action-specific provider reference;
                # otherwise derive one from the shared statement and action ID.
                per_action_reference = row['external_reference'] or 'batch:' + digest(canonical_json({'reference': reference, 'action': action}))
                prepared.append((action, request, Receipt(json.loads(row['payload']), actual_each,
                    per_action_reference, billed_day, row['request_hash'])))
            batch_id = digest(canonical_json({'actions': actions, 'policy_id': policy_id,
                'actual_each': actual_each, 'billed_day': billed_day, 'reference': reference}))
            for action, request, receipt in prepared:
                self._reconcile(db, action, request, receipt)
            db.execute('INSERT INTO paid_batch_reconciliations VALUES(?,?,?,?,?,?,?)',
                (batch_id, policy_id, canonical_json(actions), actual_each, billed_day,
                 reference, datetime.now(timezone.utc).isoformat()))
        return {'batch_id': batch_id, 'actions': len(actions), 'actual_total': actual_each * len(actions), 'billed_day': billed_day}
