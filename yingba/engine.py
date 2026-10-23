import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .normalize import keys,domain,name,email,source_emails
from .budget import BudgetPause
from .adapters import MockSource,Disconnected,ConnectionPause,TransientError

def stamp(): return datetime.now(timezone.utc).isoformat()
def event(db, task, kind, detail): db.execute('INSERT INTO events(task,at,kind,detail) VALUES(?,?,?,?)',(task,stamp(),kind,detail))

QUALIFIED_ROWS = """SELECT c.*,e.url,e.collected evidence_time,e.basis,e.body_hash,m.address
 FROM companies c JOIN tasks t ON t.id=c.task
 JOIN emails m ON m.company=c.id
 JOIN evidence e ON e.id=m.evidence AND e.company=c.id
 WHERE c.task=? AND c.stage='qualified' AND c.verified=1
 AND e.simulation=(t.mode='simulation') AND m.address!='' AND e.body!='' AND e.basis!=''
 ORDER BY c.id,m.address"""

def qualified_count(db,task):
    return db.execute('SELECT COUNT(DISTINCT id) FROM ('+QUALIFIED_ROWS+')',(task,)).fetchone()[0]

class Engine:
    # Three bounded 30s public fetches + up to five 15s CLI preflight calls
    # + a 180s structured review fit within 345s. Leave scheduling margin
    # without allowing an unbounded zombie worker to retain a task forever.
    LEASE_SECONDS=600
    def __init__(self,store,source=None,clock=time.time,source_factory=None):
        self.store=store; self.source=source; self.source_factory=source_factory; self.clock=clock; self.owner=uuid.uuid4().hex
        # Upgrade old demo identities without letting synthetic runs reserve
        # an identity in a later real campaign.
        with self.store.transaction() as db:
            db.execute("""INSERT OR IGNORE INTO identities(kind,value,company,historical)
                SELECT 'simulation:'||i.kind,i.value,i.company,0 FROM identities i
                JOIN companies c ON c.id=i.company JOIN tasks t ON t.id=c.task
                WHERE t.mode='simulation' AND i.historical=0 AND i.kind NOT LIKE 'simulation:%'""")
            db.execute("""DELETE FROM identities WHERE historical=0 AND kind NOT LIKE 'simulation:%'
                AND company IN (SELECT c.id FROM companies c JOIN tasks t ON t.id=c.task WHERE t.mode='simulation')""")
    def create(self,target=1000,mode='real',config=None):
        if mode not in ('real','simulation') or type(target) is not int or not 1<=target<=100000: raise ValueError('任务参数无效')
        task=uuid.uuid4().hex
        with self.store.transaction() as db:
            db.execute('INSERT INTO tasks(id,mode,target,status,created) VALUES(?,?,?,?,?)',(task,mode,target,'ready',stamp()))
            if config is not None:
                db.execute('INSERT INTO task_settings(task_id,config) VALUES(?,?)',(task,json.dumps(config,ensure_ascii=False,sort_keys=True)))
            event(db,task,'created','模拟数据不代表真实客户' if mode=='simulation' else '真实采集尚未连接')
        return task
    def pause(self,task,reason='用户暂停'):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='paused',reason=? WHERE id=? AND status NOT IN ('complete','simulation_complete')",(reason,task))
            if db.execute('SELECT changes()').fetchone()[0]:event(db,task,'paused',reason)
    def resume(self,task):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='ready',reason='' WHERE id=? AND status IN ('paused','insufficient')",(task,))
    def counts(self,task):
        with self.store.connect() as db:
            db.execute('BEGIN')
            t=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            if not t:raise ValueError('任务不存在')
            stages={r['stage']:r['n'] for r in db.execute('SELECT stage,COUNT(*) n FROM companies WHERE task=? GROUP BY stage',(task,))}
            verified=db.execute('SELECT COUNT(*) FROM companies WHERE task=? AND verified=1',(task,)).fetchone()[0]
            qualified=qualified_count(db,task)
        return {'候选':stages.get('candidate',0),'已核验':verified,'合格有邮箱':qualified,'排除':stages.get('excluded',0),'距目标差额':max(0,t['target']-qualified)}
    def step(self,task):
        now=self.clock()
        with self.store.transaction() as db:
            t=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            if not t or t['status'] not in ('ready','running'): return False
            if t['owner'] and t['lease']>now: return False
            candidate=db.execute("SELECT * FROM companies WHERE task=? AND stage='candidate' ORDER BY id LIMIT 1",(task,)).fetchone()
            action=f'verify:{candidate["id"]}' if candidate else f'page:{t["page"]}'
            attempt=db.execute('SELECT * FROM attempts WHERE task=? AND action=?',(task,action)).fetchone()
            if attempt and attempt['next_at']>now: return False
            db.execute("UPDATE tasks SET owner=?,lease=?,status='running' WHERE id=?",(self.owner,now+self.LEASE_SECONDS,task))
        try:
            source=self.source or (self.source_factory(dict(t)) if self.source_factory else (MockSource() if t['mode']=='simulation' else Disconnected()))
            if candidate:
                result=source.verify(json.loads(candidate['raw']))
                with self.store.transaction() as db:
                    self._guard(db,task)
                    self._verify(db,candidate,result,t['mode'])
            else:
                count=self.counts(task)['合格有邮箱']
                if count>=t['target'] or t['exhausted']:
                    with self.store.transaction() as db:
                        self._guard(db,task)
                        count=qualified_count(db,task)
                        status=('simulation_complete' if t['mode']=='simulation' else 'complete') if count>=t['target'] else 'insufficient'
                        db.execute('UPDATE tasks SET status=?,reason=?,owner=NULL,lease=0 WHERE id=?',(status,'模拟验收达标，非真实客户' if status=='simulation_complete' else ('来源已耗尽，尚未达标' if status=='insufficient' else '验收通过'),task))
                        event(db,task,status,f'合格公司={count}')
                    return False
                if t['page']>1000:raise ConnectionPause('已达到1000页安全上限，需复核筛选条件后继续')
                records,exhausted=source.search(t['page'])
                if not isinstance(records,list) or type(exhausted) is not bool or len(records)>10000:
                    raise ConnectionPause('来源分页结构无效，已停止；未推进页码')
                if not records and not exhausted:
                    raise ConnectionPause('来源返回空页但未确认耗尽，需核对分页后继续')
                with self.store.transaction() as db:
                    self._guard(db,task)
                    before=db.execute('SELECT COUNT(*) FROM companies WHERE task=?',(task,)).fetchone()[0]
                    for record in records: self._ingest(db,task,t['page'],record)
                    db.execute('UPDATE tasks SET page=page+1,exhausted=? WHERE id=?',(int(exhausted),task))
                    event(db,task,'page',f'page={t["page"]}, records={len(records)}')
                    after=db.execute('SELECT COUNT(*) FROM companies WHERE task=?',(task,)).fetchone()[0]
                    if after==before and not exhausted:
                        # Durable consecutive no-progress guard; do not pay for
                        # hundreds of repeated/invalid pages while chasing target.
                        previous=db.execute("SELECT kind FROM events WHERE task=? AND kind IN ('page_progress','page_no_progress') ORDER BY id DESC LIMIT 2",(task,)).fetchall()
                        event(db,task,'page_no_progress',f'page={t["page"]}')
                        if len(previous)==2 and all(row['kind']=='page_no_progress' for row in previous):
                            db.execute("UPDATE tasks SET status='paused',reason='连续3页没有新增公司，请复核筛选与历史去重后继续' WHERE id=?",(task,))
                    else:event(db,task,'page_progress',f'page={t["page"]}')
            with self.store.transaction() as db:
                db.execute('DELETE FROM attempts WHERE task=? AND action=?',(task,action))
            return True
        except TransientError:
            with self.store.transaction() as db:
                current=db.execute('SELECT status,owner FROM tasks WHERE id=?',(task,)).fetchone()
                if not current or current['owner']!=self.owner or current['status']!='running':return False
                n=(attempt['count'] if attempt else 0)+1
                db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)',(task,action,n,now+min(60,2**n)))
            if n>=3:self._pause_claim(task,'临时故障重试3次仍失败，请诊断后继续')
            return False
        except (ConnectionPause,BudgetPause) as exc:
            self._pause_claim(task,str(exc)); return False
        except Exception:
            self._pause_claim(task,'处理失败，状态已保留；运行诊断后继续（未记录敏感原文）'); return False
        finally:
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET owner=NULL,lease=0 WHERE id=? AND owner=?',(task,self.owner))
    def _pause_claim(self,task,reason):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='paused',reason=? WHERE id=? AND owner=? AND status='running'",(reason,task,self.owner))
            if db.execute('SELECT changes()').fetchone()[0]:event(db,task,'paused',reason)
    def _guard(self,db,task):
        t=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if t['status']!='running' or t['owner']!=self.owner or t['lease']<self.clock(): raise ConnectionPause('任务已暂停或租约失效，结果未提交')
    def _ingest(self,db,task,page,r):
        raw=json.dumps(r,ensure_ascii=False)
        if not isinstance(r,dict) or not isinstance(r.get('name'),str) or not isinstance(r.get('domain'),str):
            db.execute('INSERT INTO raw_records(task,page,raw,outcome,collected) VALUES(?,?,?,?,?)',(task,page,raw,'排除：记录结构或名称/域名类型无效',stamp()));return
        identities=keys(r)
        mode=db.execute('SELECT mode FROM tasks WHERE id=?',(task,)).fetchone()[0]
        prefix='simulation:' if mode=='simulation' else ''
        duplicate=None
        for k,v in identities:
            hit=db.execute('SELECT * FROM identities WHERE value=? AND (kind=? OR (kind=? AND historical=1)) ORDER BY historical DESC LIMIT 1',(v,prefix+k,k)).fetchone()
            if hit: duplicate=hit;break
        if duplicate:
            # A name-only collision is insufficient to assert that a new domain
            # or Apollo ID belongs to the old entity. Preserve the record, never
            # propagate unverified aliases into the global identity index.
            db.execute('INSERT INTO raw_records(task,page,raw,outcome,company,collected) VALUES(?,?,?,?,?,?)',(task,page,raw,'历史或当批重复',duplicate['company'],stamp()));return
        if not name(r.get('name')) or not domain(r.get('domain')):
            db.execute('INSERT INTO raw_records(task,page,raw,outcome,collected) VALUES(?,?,?,?,?)',(task,page,raw,'排除：缺公司名称或域名',stamp()));return
        cid=db.execute('INSERT INTO companies(task,name,domain,apollo,name_key,stage,raw,collected) VALUES(?,?,?,?,?,?,?,?)',(task,r['name'],domain(r['domain']),str(r.get('apollo_id','')),name(r['name']),'candidate',raw,stamp())).lastrowid
        for k,v in identities:db.execute('INSERT INTO identities VALUES(?,?,?,0)',(prefix+k,v,cid))
        db.execute('INSERT INTO raw_records(task,page,raw,outcome,company,collected) VALUES(?,?,?,?,?,?)',(task,page,raw,'候选',cid,stamp()))
    def _verify(self,db,c,r,mode):
        if not isinstance(r,dict): raise ValueError('核实输出无效')
        if type(r.get('simulation')) is not bool or any(not isinstance(r.get(k),str) for k in ('url','basis','text','category')) or not isinstance(r.get('emails'),list):raise ValueError('核实输出字段类型无效')
        if len(r['text'])>500000 or len(r['emails'])>200:raise ValueError('核实结果超过安全大小上限')
        simulation=r['simulation']
        if simulation != (mode=='simulation'):raise ValueError('模拟和真实证据禁止混用')
        if mode=='real' and r.get('review_required') is not False:
            raise ConnectionPause('公开页面已抓取，但公司主体、业务和商务邮箱的订阅审核尚未完成')
        def source_url(url):
            try:
                parsed=urlsplit(url)
                return (domain(url)==c['domain'] and parsed.scheme in (('https','http') if simulation else ('https',))
                        and parsed.port in ((None,80,443) if simulation else (None,443)) and not parsed.fragment)
            except ValueError:return False
        url=r['url'];basis=r['basis']
        valid=(r.get('identity_confirmed') is True and r.get('business_confirmed') is True and r['category'] in ('润滑油 OEM/ODM','润滑油进口商','润滑油经销商','润滑油相关渠道') and source_url(url) and bool(basis.strip()) and bool(r['text'].strip()))
        pages=r.get('evidence_pages')
        if pages is None:
            if not simulation:raise ValueError('真实核实必须提供逐页证据')
            pages=[{'url':url,'basis':basis,'text':r['text'],'emails':r['emails']}]
        if not isinstance(pages,list) or not 1<=len(pages)<=10:raise ValueError('逐页证据结构无效')
        if any(not isinstance(page,dict) or any(not isinstance(page.get(k),str) for k in ('url','text','basis')) or not isinstance(page.get('emails'),list) or len(page['emails'])>200 for page in pages):raise ValueError('逐页证据字段无效')
        if sum(len(page['text']) for page in pages)>500000:raise ValueError('逐页证据超过大小限制')
        valid=valid and url in {page['url'] for page in pages}
        claimed={email(x) for x in r['emails'] if isinstance(x,str)}
        attributed={}
        for page in pages:
            # Preserve every inspected page against this company, including an
            # exclusion; mailbox rows are linked to their actual source page.
            eid=db.execute('INSERT INTO evidence(company,url,collected,basis,body_hash,body,simulation) VALUES(?,?,?,?,?,?,?)',(c['id'],page['url'],stamp(),page['basis'],hashlib.sha256(page['text'].encode()).hexdigest(),page['text'],int(simulation))).lastrowid
            observed=source_emails(page['text'])
            if not source_url(page['url']) or not page['basis'].strip():continue
            for value in page['emails']:
                address=email(value) if isinstance(value,str) else ''
                if address and address in claimed and address in observed and address.split('@')[1]==c['domain']:
                    attributed.setdefault(address,eid)
        addresses=set(attributed)
        prefix='simulation:' if simulation else ''
        historical={a for a in addresses if db.execute("SELECT 1 FROM identities WHERE kind='email' AND value=? AND historical=1",(a,)).fetchone()}
        previous={a for a in addresses if db.execute("SELECT 1 FROM identities WHERE kind=? AND value=? AND company!=?",(prefix+'email',a,c['id'])).fetchone()}
        # An observed historical mailbox identifies an already-known company;
        # finding a second new mailbox must not count that company as new.
        if historical or previous:addresses=set()
        if valid and addresses:
            for a in addresses:
                db.execute('INSERT OR IGNORE INTO emails VALUES(?,?,?)',(a,c['id'],attributed[a]))
                db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,?,0)',(prefix+'email',a,c['id']))
            stage='qualified';reason=r['category']+'；公开来源核实，未做投递验证'
        else:stage='excluded';reason='主体、业务或证据未确认' if not valid else ('邮箱已存在于历史或其他公司，排除重复公司' if historical or previous else '未发现可归属且有原文支持的商务邮箱')
        db.execute('UPDATE companies SET stage=?,reason=?,verified=1 WHERE id=?',(stage,reason,c['id']))
        db.execute("UPDATE raw_records SET outcome=? WHERE task=? AND company=? AND outcome='候选'",(stage,c['task'],c['id']))
        event(db,c['task'],stage,f'company={c["id"]} {reason}')
    def run(self,task,max_steps=10000):
        for _ in range(max_steps):
            if not self.step(task): break
        return self.counts(task)
