import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .normalize import keys,domain,name,email
from .budget import BudgetPause
from .adapters import MockSource,Disconnected,ConnectionPause,TransientError

def stamp(): return datetime.now(timezone.utc).isoformat()
def event(db, task, kind, detail): db.execute('INSERT INTO events(task,at,kind,detail) VALUES(?,?,?,?)',(task,stamp(),kind,detail))

class Engine:
    def __init__(self,store,source=None,clock=time.time):
        self.store=store; self.source=source; self.clock=clock; self.owner=uuid.uuid4().hex
    def create(self,target=1000,mode='real'):
        if mode not in ('real','simulation') or not isinstance(target,int) or not 1<=target<=100000: raise ValueError('任务参数无效')
        task=uuid.uuid4().hex
        with self.store.transaction() as db:
            db.execute('INSERT INTO tasks(id,mode,target,status,created) VALUES(?,?,?,?,?)',(task,mode,target,'ready',stamp()))
            event(db,task,'created','模拟数据不代表真实客户' if mode=='simulation' else '真实采集尚未连接')
        return task
    def pause(self,task,reason='用户暂停'):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='paused',reason=? WHERE id=? AND status NOT IN ('complete','simulation_complete')",(reason,task))
            event(db,task,'paused',reason)
    def resume(self,task):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='ready',reason='' WHERE id=? AND status IN ('paused','insufficient')",(task,))
    def counts(self,task):
        rows=self.store.rows('SELECT stage,COUNT(*) n FROM companies WHERE task=? GROUP BY stage',(task,))
        stages={r['stage']:r['n'] for r in rows}
        t=self.store.rows('SELECT * FROM tasks WHERE id=?',(task,))[0]
        verified=self.store.rows('SELECT COUNT(*) n FROM companies WHERE task=? AND verified=1',(task,))[0]['n']
        qualified=stages.get('qualified',0)
        return {'候选':stages.get('candidate',0),'已核验':verified,'合格有邮箱':qualified,'排除':stages.get('excluded',0),'距目标差额':max(0,t['target']-qualified)}
    def step(self,task):
        now=self.clock()
        with self.store.transaction() as db:
            t=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            if not t or t['status'] not in ('ready','running'): return False
            if t['owner'] and t['owner']!=self.owner and t['lease']>now: return False
            db.execute("UPDATE tasks SET owner=?,lease=?,status='running' WHERE id=?",(self.owner,now+300,task))
            candidate=db.execute("SELECT * FROM companies WHERE task=? AND stage='candidate' ORDER BY id LIMIT 1",(task,)).fetchone()
            action=f'verify:{candidate["id"]}' if candidate else f'page:{t["page"]}'
            attempt=db.execute('SELECT * FROM attempts WHERE task=? AND action=?',(task,action)).fetchone()
            if attempt and attempt['next_at']>now: return False
        source=self.source or (MockSource() if t['mode']=='simulation' else Disconnected())
        try:
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
                        status=('simulation_complete' if t['mode']=='simulation' else 'complete') if count>=t['target'] else 'insufficient'
                        db.execute('UPDATE tasks SET status=?,reason=?,owner=NULL,lease=0 WHERE id=?',(status,'模拟验收达标，非真实客户' if status=='simulation_complete' else ('来源已耗尽，尚未达标' if status=='insufficient' else '验收通过'),task))
                        event(db,task,status,f'合格公司={count}')
                    return False
                if t['page']>1000:raise ConnectionPause('已达到1000页安全上限，需复核筛选条件后继续')
                records,exhausted=source.search(t['page'])
                with self.store.transaction() as db:
                    self._guard(db,task)
                    for record in records: self._ingest(db,task,t['page'],record)
                    db.execute('UPDATE tasks SET page=page+1,exhausted=? WHERE id=?',(int(exhausted),task))
                    event(db,task,'page',f'page={t["page"]}, records={len(records)}')
            with self.store.transaction() as db:
                db.execute('DELETE FROM attempts WHERE task=? AND action=?',(task,action))
            return True
        except TransientError:
            with self.store.transaction() as db:
                n=(attempt['count'] if attempt else 0)+1
                db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)',(task,action,n,now+min(60,2**n)))
            if n>=3:self.pause(task,'临时故障重试3次仍失败，请诊断后继续')
            return False
        except (ConnectionPause,BudgetPause) as exc:
            self.pause(task,str(exc)); return False
        except Exception:
            self.pause(task,'处理失败，状态已保留；运行诊断后继续（未记录敏感原文）'); return False
        finally:
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET owner=NULL,lease=0 WHERE id=? AND owner=?',(task,self.owner))
    def _guard(self,db,task):
        t=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        if t['status']!='running' or t['owner']!=self.owner or t['lease']<self.clock(): raise ConnectionPause('任务已暂停或租约失效，结果未提交')
    def _ingest(self,db,task,page,r):
        identities=keys(r); raw=json.dumps(r,ensure_ascii=False)
        duplicate=None
        for k,v in identities:
            hit=db.execute('SELECT * FROM identities WHERE kind=? AND value=?',(k,v)).fetchone()
            if hit: duplicate=hit;break
        if duplicate:
            for k,v in identities:
                db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,?,?)',(k,v,duplicate['company'],duplicate['historical']))
            db.execute('INSERT INTO raw_records(task,page,raw,outcome,company,collected) VALUES(?,?,?,?,?,?)',(task,page,raw,'历史或当批重复',duplicate['company'],stamp()));return
        if not name(r.get('name')) or not domain(r.get('domain')):
            db.execute('INSERT INTO raw_records(task,page,raw,outcome,collected) VALUES(?,?,?,?,?)',(task,page,raw,'排除：缺公司名称或域名',stamp()));return
        cid=db.execute('INSERT INTO companies(task,name,domain,apollo,name_key,stage,raw,collected) VALUES(?,?,?,?,?,?,?,?)',(task,r['name'],domain(r['domain']),str(r.get('apollo_id','')),name(r['name']),'candidate',raw,stamp())).lastrowid
        for k,v in identities:db.execute('INSERT INTO identities VALUES(?,?,?,0)',(k,v,cid))
        db.execute('INSERT INTO raw_records(task,page,raw,outcome,company,collected) VALUES(?,?,?,?,?,?)',(task,page,raw,'候选',cid,stamp()))
    def _verify(self,db,c,r,mode):
        if not isinstance(r,dict): raise ValueError('核实输出无效')
        simulation=bool(r.get('simulation'))
        if simulation != (mode=='simulation'):raise ValueError('模拟和真实证据禁止混用')
        url=str(r.get('url','')); basis=str(r.get('basis',''))
        host=domain(url)
        same_host=host==c['domain'] or host.endswith('.'+c['domain'])
        valid=(r.get('identity_confirmed') is True and r.get('business_confirmed') is True and r.get('category') in ('润滑油 OEM/ODM','润滑油进口商','润滑油经销商','润滑油相关渠道') and urlsplit(url).scheme in ('https','http') and same_host and bool(basis.strip()) and bool(r.get('text')))
        eid=db.execute('INSERT INTO evidence(company,url,collected,basis,body_hash,body,simulation) VALUES(?,?,?,?,?,?,?)',(c['id'],url,stamp(),basis,hashlib.sha256(str(r.get('text','')).encode()).hexdigest(),str(r.get('text',''))[:500000],int(simulation))).lastrowid
        addresses={email(x) for x in r.get('emails',[]) if isinstance(x,str)}
        # Conservative attribution: same-domain public business mailbox only.
        addresses={a for a in addresses if a and domain(a.split('@')[1])==c['domain'] and a in str(r.get('text','')).casefold()}
        historical={a for a in addresses if db.execute("SELECT 1 FROM identities WHERE kind='email' AND value=? AND historical=1",(a,)).fetchone()}
        addresses-=historical
        if valid and addresses:
            for a in addresses:db.execute('INSERT OR IGNORE INTO emails VALUES(?,?,?)',(a,c['id'],eid))
            stage='qualified';reason=r['category']+'；公开来源核实，未做投递验证'
        else:stage='excluded';reason='主体、业务或证据未确认' if not valid else ('邮箱已存在于历史，排除重复公司' if historical else '未发现可归属且有原文支持的商务邮箱')
        db.execute('UPDATE companies SET stage=?,reason=?,verified=1 WHERE id=?',(stage,reason,c['id']))
        event(db,c['task'],stage,f'company={c["id"]} {reason}')
    def run(self,task,max_steps=10000):
        for _ in range(max_steps):
            if not self.step(task): break
        return self.counts(task)
