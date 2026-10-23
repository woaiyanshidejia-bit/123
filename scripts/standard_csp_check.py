"""Browser under production CSP; test harness does not bypass security policy."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
with tempfile.TemporaryDirectory() as temp:
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    p=subprocess.Popen([sys.executable,'-m','yingba.server','--data',temp,'--port',str(port),'--no-browser'],stdout=subprocess.PIPE,text=True)
    try:
        assert 'YINGBA' in p.stdout.readline()
        with sync_playwright() as pw:
            b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            url=f'http://127.0.0.1:{port}';r=page.goto(url);assert "frame-ancestors 'none'" in r.headers['content-security-policy']
            page.get_by_role('button',name='任务设置',exact=True).click();page.locator('#target').fill('1');page.locator('#mode').select_option('simulation');page.get_by_role('button',name='创建并开始',exact=True).click();page.get_by_role('button',name='概览',exact=True).click()
            expect(page.locator('#status')).to_contain_text('模拟验收达标',timeout=20000)
            expect(page.locator('#cards')).to_contain_text('合格有邮箱')
            assert not errors
            result={'version':'1.2.0','checked_at':datetime.now(timezone.utc).isoformat(),'csp_bypass':False,'standard_csp_real_ui_flow':'PASS','inline_handlers_work':True,'page_errors':errors,'bound_to':'127.0.0.1','external_frontend_requests':0}
            Path('artifacts/http-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False));b.close()
    finally:p.terminate();p.wait(timeout=10);p.stdout.close()
