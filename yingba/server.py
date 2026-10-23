import argparse
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .db import Store
from .engine import Engine
from .exchange import export,import_history
from .diagnostics import diagnose
from .lock import InstanceLock
from .budget import Budget, BudgetPause, beijing_day
from .adapters import ConnectionPause
from .paid import PaidGateway, valid_reference
from .runtime import Runtime, settings

ROOT=Path(__file__).parent

def serve(data,port=8765,open_browser=True):
    data=Path(data).resolve();data.mkdir(parents=True,exist_ok=True)
    with InstanceLock(data/'instance.lock'):
        store=Store(data/'workbench.db'); runtime=Runtime(store); engine=Engine(store,source_factory=runtime.source_for_task); token=secrets.token_urlsafe(32); stop=threading.Event()
        def worker():
            while not stop.wait(.25):
                for t in store.rows("SELECT id FROM tasks WHERE status IN ('ready','running') ORDER BY created"):
                    try:engine.step(t['id'])
                    except Exception:engine.pause(t['id'],'后台处理异常，状态已保留；请运行诊断后继续')
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup();self.connection.settimeout(20)
            def log_message(self,*args):pass # No request data or credentials in logs.
            def send(self,status,body,typ='application/json; charset=utf-8'):
                body=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(body)))
                self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'")
                self.end_headers();self.wfile.write(body)
            def valid_host(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
            def do_GET(self):
                if not self.valid_host():return self.send(403,{'error':'仅允许本机访问'})
                parsed=urlsplit(self.path)
                if parsed.path=='/':
                    body=(ROOT/'ui.html').read_text(encoding='utf-8').replace('__TOKEN__',token).encode()
                    return self.send(200,body,'text/html; charset=utf-8')
                if parsed.path=='/api/state':
                    tasks=store.rows('SELECT * FROM tasks ORDER BY created DESC')
                    for t in tasks:t['counts']=engine.counts(t['id'])
                    return self.send(200,{'tasks':tasks,'companies':store.rows('SELECT id,task,name,domain,stage,reason,verified FROM companies ORDER BY id DESC'),
                        'emails':store.rows("SELECT m.*,c.name,c.task FROM emails m JOIN companies c ON m.company=c.id WHERE c.stage='qualified' AND c.verified=1 ORDER BY address"),
                        'evidence':store.rows('SELECT e.id,e.company,e.url,e.collected,e.basis,e.body_hash,e.simulation,c.name,c.task FROM evidence e JOIN companies c ON e.company=c.id ORDER BY e.id DESC'),
                        'connections':runtime.status(), 'settings':store.rows('SELECT * FROM task_settings'), 'budget':{'day':beijing_day(),'ledger':store.rows('SELECT * FROM budgets WHERE day=?',(beijing_day(),)), 'pending':PaidGateway(store).pending()},'events':store.rows('SELECT * FROM events ORDER BY id DESC LIMIT 100'),'exports':[p.name for p in sorted((data/'exports').glob('*'))] if (data/'exports').exists() else []})
                if parsed.path=='/api/diagnostics':return self.send(200,diagnose(store,runtime))
                if parsed.path.startswith('/download/'):
                    from urllib.parse import unquote
                    filename=unquote(parsed.path.removeprefix('/download/'))
                    if '/' in filename or '\\' in filename or filename.startswith('.'):return self.send(400,{'error':'无效文件名'})
                    path=data/'exports'/filename
                    if not path.is_file():return self.send(404,{'error':'文件不存在'})
                    return self.send(200,path.read_bytes(),'application/octet-stream')
                return self.send(404,{'error':'不存在'})
            def do_POST(self):
                if not self.valid_host() or self.headers.get('Origin') not in (None,f'http://127.0.0.1:{self.server.server_port}') or not hmac.compare_digest(self.headers.get('X-Yingba-Token',''),token):return self.send(403,{'error':'请求校验失败，请刷新本机页面'})
                action=self.path
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=15_000_000:raise ValueError('请求过大或为空')
                    r=json.loads(self.rfile.read(size));action=self.path
                    if action in ('/api/pause','/api/resume') and not store.rows('SELECT 1 FROM tasks WHERE id=?',(r.get('task'),)):
                        raise ValueError('任务不存在')
                    if action=='/api/create':
                        config=settings(r.get('settings')) if r.get('mode','real')=='real' else None
                        result={'id':engine.create(r.get('target',1000),r.get('mode','real'),config=config)}
                    elif action=='/api/pause':engine.pause(r['task']);result={'ok':True}
                    elif action=='/api/resume':engine.resume(r['task']);result={'ok':True}
                    elif action=='/api/export':
                        p,a=export(store,r['task'],data/'exports');result={'files':[p.name,a.name]}
                    elif action=='/api/budget/confirm':
                        if r.get('confirmed') is not True:raise ValueError('未确认全账号用量')
                        Budget(store).confirm(r['used']);result={'ok':True}
                    elif action=='/api/budget/settle':
                        if r.get('confirmed') is not True:raise ValueError('须核实实际扣费和日期')
                        PaidGateway(store).reconcile_captured(r['action'],r['actual'],r['billed_day'],r['reference']);result={'ok':True}
                    elif action=='/api/budget/resolve-missing':
                        if r.get('confirmed') is not True or not str(r.get('reference','')).strip():raise ValueError('须核实实际扣费及动作专属依据')
                        valid_reference(r['reference'])
                        with store.transaction() as db:
                            if db.execute('SELECT 1 FROM tasks WHERE owner IS NOT NULL AND lease>?',(time.time(),)).fetchone():raise BudgetPause('仍有请求在途，请等待任务暂停或租约到期后核清缺失响应')
                            if db.execute('SELECT 1 FROM paid_observations WHERE action=?',(r['action'],)).fetchone():raise BudgetPause('该动作已保存响应，请使用已保存响应对账')
                            Budget(store)._settle(db,r['action'],r['actual'],r['billed_day'])
                            db.execute("INSERT INTO events(task,at,kind,detail) VALUES(NULL,datetime('now'),?,?)",('manual_reconcile',json.dumps({'action':r['action'],'reference':r['reference']},ensure_ascii=False)))
                        result={'ok':True,'reason':'账目已核实；未恢复缺失响应，原动作禁止重发，请保留追溯并新建任务'}
                    elif action=='/api/connect':result=runtime.connect(r.get('api_key'),r.get('account_label'),r.get('authorized'))
                    elif action=='/api/disconnect':runtime.disconnect();result={'ok':True}
                    elif action=='/api/codex/validate':result=runtime.validate_codex(authorized=r.get('authorized') is True)
                    elif action=='/api/history':
                        import base64
                        raw=base64.b64decode(r['content'],validate=True)
                        if len(raw)>10_000_000:raise ValueError('历史文件最大10MB')
                        # Private temporary copy; preserve user's original, never execute content.
                        import tempfile
                        with tempfile.TemporaryDirectory(dir=data) as temp:
                            p=Path(temp)/'history.xlsx';p.write_bytes(raw)
                            result={'rows':import_history(store,p,r.get('mapping'))}
                    else:return self.send(404,{'error':'未知操作'})
                    return self.send(200,result)
                except (BudgetPause,ConnectionPause) as exc:return self.send(409,{'error':str(exc)})
                except ValueError as exc:return self.send(400,{'error':str(exc)[:300] if action not in ('/api/connect','/api/history') else '参数无效，请检查输入；密钥及文件内容未记录'})
                except (KeyError,TypeError):return self.send(400,{'error':'参数或历史文件格式无效，请检查字段映射'})
                except Exception:return self.send(409,{'error':'操作失败，未标记成功；请查看诊断和文件格式'})
        server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        threading.Thread(target=worker,daemon=True).start()
        url=f'http://127.0.0.1:{server.server_port}'
        print('YINGBA 本地工作台：'+url,flush=True)
        if open_browser:webbrowser.open(url)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:stop.set();server.shutdown();server.server_close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',default=str(Path(__file__).resolve().parents[1]/'data'));p.add_argument('--port',type=int,default=8765);p.add_argument('--no-browser',action='store_true');p.add_argument('--diagnose',action='store_true');args=p.parse_args()
    if args.diagnose:print(json.dumps(diagnose(Store(Path(args.data)/'workbench.db')),ensure_ascii=False,indent=2))
    else:
        try:serve(args.data,args.port,not args.no_browser)
        except (RuntimeError,OSError) as exc:print(str(exc));raise SystemExit(1)
if __name__=='__main__':main()
