import sqlite3
from pathlib import Path
from contextlib import contextmanager

SCHEMA_VERSION = 3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, mode TEXT NOT NULL, target INTEGER NOT NULL CHECK(target>0), status TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 1, exhausted INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', owner TEXT, lease REAL NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_settings(task_id TEXT PRIMARY KEY REFERENCES tasks(id), config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_pages(task_id TEXT NOT NULL REFERENCES tasks(id), logical_page INTEGER NOT NULL, query_index INTEGER NOT NULL, api_page INTEGER NOT NULL, action TEXT UNIQUE, payload TEXT, PRIMARY KEY(task_id,logical_page));
CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), name TEXT NOT NULL, domain TEXT NOT NULL, apollo TEXT NOT NULL, name_key TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0, raw TEXT NOT NULL, collected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS identities(kind TEXT NOT NULL, value TEXT NOT NULL, company INTEGER, historical INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,value));
CREATE TABLE IF NOT EXISTS raw_records(id INTEGER PRIMARY KEY, task TEXT NOT NULL, page INTEGER, raw TEXT NOT NULL, outcome TEXT NOT NULL, company INTEGER, collected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, company INTEGER NOT NULL REFERENCES companies(id), url TEXT NOT NULL, collected TEXT NOT NULL, basis TEXT NOT NULL, body_hash TEXT NOT NULL, body TEXT NOT NULL, simulation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS emails(address TEXT NOT NULL, company INTEGER NOT NULL REFERENCES companies(id), evidence INTEGER NOT NULL REFERENCES evidence(id), PRIMARY KEY(address,company));
CREATE TABLE IF NOT EXISTS history_imports(id INTEGER PRIMARY KEY, source TEXT NOT NULL, sha256 TEXT NOT NULL, imported TEXT NOT NULL, rows INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS companies_task_stage ON companies(task,stage,id);
CREATE INDEX IF NOT EXISTS evidence_company ON evidence(company);
CREATE INDEX IF NOT EXISTS emails_company ON emails(company);
CREATE INDEX IF NOT EXISTS raw_task ON raw_records(task);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, task TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budgets(day TEXT PRIMARY KEY, used INTEGER NOT NULL CHECK(used>=0), confirmed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS charges(action TEXT PRIMARY KEY, day TEXT NOT NULL, estimate INTEGER NOT NULL CHECK(estimate>=0), actual INTEGER, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS charge_settlements(action TEXT PRIMARY KEY REFERENCES charges(action), billed_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paid_results(action TEXT PRIMARY KEY REFERENCES charges(action), request_hash TEXT NOT NULL, payload TEXT NOT NULL, external_reference TEXT NOT NULL, collected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paid_requests(action TEXT PRIMARY KEY REFERENCES charges(action), request_hash TEXT NOT NULL, request_json TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paid_observations(action TEXT PRIMARY KEY REFERENCES charges(action), payload TEXT NOT NULL, response_hash TEXT NOT NULL, external_reference TEXT NOT NULL DEFAULT '', collected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paid_policies(id TEXT PRIMARY KEY, account_label TEXT NOT NULL, day TEXT NOT NULL, request_template TEXT NOT NULL, template_hash TEXT NOT NULL, upper_bound INTEGER NOT NULL CHECK(upper_bound>=0 AND upper_bound<=50), variable_page INTEGER NOT NULL, evidence TEXT NOT NULL, evidence_hash TEXT NOT NULL, policy_hash TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bounded_actions(action TEXT PRIMARY KEY REFERENCES charges(action), policy_id TEXT NOT NULL REFERENCES paid_policies(id), policy_hash TEXT NOT NULL, observed_at TEXT);
CREATE TABLE IF NOT EXISTS cost_assessments(assessment_id TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL, endpoint TEXT NOT NULL, account_label TEXT NOT NULL, request_hash TEXT NOT NULL, classification TEXT NOT NULL, max_points INTEGER, reason TEXT NOT NULL, evidence TEXT NOT NULL, model_review TEXT NOT NULL, policy_id TEXT, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cost_assessments_lookup ON cost_assessments(day,source,account_label,request_hash);
CREATE TABLE IF NOT EXISTS paid_batch_reconciliations(id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES paid_policies(id), actions TEXT NOT NULL, actual_each INTEGER NOT NULL, billed_day TEXT NOT NULL, reference TEXT NOT NULL, collected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_confirmations(id INTEGER PRIMARY KEY, day TEXT NOT NULL, used INTEGER NOT NULL, confirmed_at TEXT NOT NULL, basis TEXT NOT NULL DEFAULT 'manual-account-total');
CREATE INDEX IF NOT EXISTS paid_results_reference ON paid_results(external_reference);
CREATE TABLE IF NOT EXISTS verification_cache(task_id TEXT NOT NULL REFERENCES tasks(id), record_hash TEXT NOT NULL, result TEXT NOT NULL, collected TEXT NOT NULL, PRIMARY KEY(task_id,record_hash));
CREATE TABLE IF NOT EXISTS attempts(task TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(task,action));
CREATE TABLE IF NOT EXISTS apollo_imports(batch_id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL, source_url TEXT NOT NULL, source_attestation TEXT NOT NULL, imported_at TEXT NOT NULL, mapping TEXT NOT NULL, rows INTEGER NOT NULL CHECK(rows>=0));
CREATE TABLE IF NOT EXISTS apollo_import_rows(batch_id TEXT NOT NULL REFERENCES apollo_imports(batch_id), row_number INTEGER NOT NULL, raw TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(batch_id,row_number));
CREATE TABLE IF NOT EXISTS free_pages(task_id TEXT NOT NULL REFERENCES tasks(id), logical_page INTEGER NOT NULL, request_hash TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, collected TEXT NOT NULL, PRIMARY KEY(task_id,logical_page));
'''
class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:return super().__exit__(*args)
        finally:self.close()

class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
                raise RuntimeError('数据库版本高于当前程序，请使用对应新版，禁止降级改写')
            db.executescript(SCHEMA)
            # Version 1 stored request hashes only after settlement. Preserve those
            # bindings; never invent a binding for a legacy unknown action.
            db.execute('''INSERT OR IGNORE INTO paid_requests(action,request_hash,request_json,created)
                          SELECT action,request_hash,NULL,collected FROM paid_results''')
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db
    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql,args)]
