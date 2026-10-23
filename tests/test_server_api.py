"""Actual loopback HTTP process checks; never calls paid external services."""
import base64
from io import BytesIO
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from openpyxl import Workbook
from yingba.db import Store
from yingba.engine import Engine
from yingba.budget import Budget, beijing_day

class ServerAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root/'workbench.db')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.url=f'http://127.0.0.1:{port}'
        self.process=subprocess.Popen([sys.executable,'-m','yingba.server','--data',str(self.root),'--port',str(port),'--no-browser'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.assertIn('YINGBA',self.process.stdout.readline())
        html=urllib.request.urlopen(self.url).read().decode()
        self.token=re.search("const token='([^']+)'",html).group(1)
    def tearDown(self):
        self.process.terminate();self.process.wait(timeout=10);self.process.stdout.close();self.process.stderr.close();self.temp.cleanup()
    def post(self,path,payload):
        req=urllib.request.Request(self.url+path,json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Yingba-Token':self.token,'Origin':self.url})
        try:
            with urllib.request.urlopen(req) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as response:return response.code,json.load(response)
    def test_history_exclusion_disappears_from_live_mailbox_ui(self):
        e=Engine(self.store);t=e.create(15,'simulation');e.pause(t)
        # Direct fixture work uses the same engine but avoids server-worker races.
        e.resume(t);e.run(t)
        w=Workbook();w.active.append(['邮箱']);w.active.append(['sales@lube1.example']);blob=BytesIO();w.save(blob)
        code,_=self.post('/api/history',{'content':base64.b64encode(blob.getvalue()).decode(),'mapping':{'email':'邮箱'}})
        self.assertEqual(code,200)
        with urllib.request.urlopen(self.url+'/api/state') as response:state=json.load(response)
        self.assertEqual(len([x for x in state['emails'] if x['task']==t]),15)
        self.assertEqual(state['tasks'][0]['counts']['合格有邮箱'],15)
    def test_missing_response_settlement_waits_for_live_lease(self):
        e=Engine(self.store);t=e.create(1,'simulation');e.pause(t)
        Budget(self.store).confirm(0);Budget(self.store).reserve('missing-fixture',1);Budget(self.store).unknown('missing-fixture')
        with self.store.transaction() as db:db.execute('UPDATE tasks SET owner=?,lease=? WHERE id=?',('fixture',time.time()+600,t))
        payload={'action':'missing-fixture','actual':0,'billed_day':beijing_day(),'reference':'fixture-bill-only','confirmed':True}
        self.assertEqual(self.post('/api/budget/resolve-missing',payload)[0],409)
        self.assertEqual(self.store.rows('SELECT status FROM charges')[0]['status'],'unknown')
        with self.store.transaction() as db:db.execute('UPDATE tasks SET owner=NULL,lease=0 WHERE id=?',(t,))
        self.assertEqual(self.post('/api/budget/resolve-missing',payload)[0],200)
        self.assertEqual(self.store.rows('SELECT status FROM charges')[0]['status'],'settled')
        self.assertEqual(self.store.rows('SELECT * FROM paid_results'),[])
    def test_nonexistent_task_never_reports_success(self):
        for path in ['/api/pause','/api/resume']:
            self.assertEqual(self.post(path,{'task':'absent'})[0],400)
    def test_live_and_subscription_calls_require_explicit_authorization(self):
        self.assertEqual(self.post('/api/connect',{'api_key':'synthetic','account_label':'fixture','authorized':False})[0],409)
        self.assertEqual(self.post('/api/codex/validate',{'authorized':False})[0],409)
        self.assertEqual(self.store.rows('SELECT * FROM charges'),[])

if __name__=='__main__':unittest.main()
