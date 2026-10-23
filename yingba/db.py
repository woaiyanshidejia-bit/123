import sqlite3
from pathlib import Path
from contextlib import contextmanager

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, mode TEXT NOT NULL, target INTEGER NOT NULL CHECK(target>0), status TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 1, exhausted INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', owner TEXT, lease REAL NOT NULL DEFAULT 0, created TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS attempts(task TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(task,action));
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
            db.executescript(SCHEMA)
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
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
