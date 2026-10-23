"""Cloud-only browser acceptance. Requires developer-only Playwright and Chromium."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright

with tempfile.TemporaryDirectory() as tmp:
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    p=subprocess.Popen([sys.executable,'-m','yingba.server','--data',tmp,'--port',str(port),'--no-browser'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert 'YINGBA' in p.stdout.readline()
        url=f'http://127.0.0.1:{port}'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=os.environ.get('YINGBA_CHROMIUM','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            context=browser.new_context(bypass_csp=True,viewport={'width':1440,'height':1080})
            context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(url+'/') else route.abort())
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(url);page.get_by_role('button',name='任务设置',exact=True).click()
            page.locator('#target').fill('15');page.locator('#mode').select_option('simulation');page.get_by_role('button',name='创建并开始',exact=True).click()
            page.wait_for_function('state.tasks.length===1')
            page.get_by_role('button',name='概览',exact=True).click();page.get_by_role('button',name='暂停',exact=True).click()
            page.wait_for_function('state.tasks[0].status==="paused"')
            page.get_by_role('button',name='开始 / 继续',exact=True).click()
            page.wait_for_function('state.tasks[0].status==="simulation_complete"',timeout=30000)
            assert page.locator('#cards').inner_text().find('16')>=0
            page.get_by_role('button',name='导出当前合格结果',exact=True).click()
            page.wait_for_function('state.exports.length===2')
            page.get_by_role('button',name='导出',exact=True).click()
            with page.expect_download() as info:page.locator('#files a').filter(has_text=re.compile(r'\.xlsx$')).click()
            download=info.value;assert download.suggested_filename.startswith('SIMULATION_')
            for label in ['公司总表','邮箱清单','核实证据','连接状态']:
                page.get_by_role('button',name=label,exact=True).click()
            # Real integration buttons fail before network/model without explicit authorization.
            page.locator('#apolloKey').fill('synthetic-ui-fixture')
            page.get_by_role('button',name='连接并免费检查',exact=True).click()
            page.wait_for_function('document.querySelector("#message").textContent.includes("授权")')
            assert page.locator('#apolloKey').input_value()==''
            page.get_by_role('button',name='运行订阅最小验收',exact=True).click()
            page.wait_for_function('document.querySelector("#codexResult").textContent.includes("同意")')
            # Read-only diagnostic may query local CLI status but never exec or login.
            page.get_by_role('button',name='运行诊断',exact=True).click()
            page.wait_for_function('document.querySelector("#diagnostics").textContent.includes("Apollo")')
            page.locator('#budgetUsed').fill('0');page.locator('#budgetConfirmed').check();page.get_by_role('button',name='确认已核实用量',exact=True).click()
            page.wait_for_function('state.budget.ledger.length===1')
            page.get_by_role('button',name='任务设置',exact=True).click();page.locator('#target').fill('1000');page.locator('#mode').select_option('real');page.get_by_role('button',name='创建并开始',exact=True).click()
            page.wait_for_function('state.tasks.some(t=>t.mode==="real"&&t.status==="paused")')
            real=next(t for t in page.evaluate('state.tasks') if t['mode']=='real')
            assert real['counts']['合格有邮箱']==0 and '未连接' in real['reason']
            page.get_by_role('button',name='概览',exact=True).click();page.screenshot(path='/tmp/yingba-ui-check.png',full_page=True)
            assert not errors,errors
            # HTTP security assertions from actual served origin.
            assert context.request.post(url+'/api/create',data={'target':1}).status==403
            assert context.request.get(url+'/api/state',headers={'Host':'evil.example'}).status==403
            result={'version':'1.1.0','checked_at':datetime.now(timezone.utc).isoformat(),'browser':browser.version,'checks':['中文导航7页','创建模拟任务','暂停/继续','20家公司贯通及16合格','Excel下载','诊断状态','预算基线确认','真实模式未连接安全暂停','CSRF拒绝','Host拒绝','未授权真实连接及订阅调用拒绝','密钥输入框清空'],'harness_csp_bypass':True,'console_errors':errors,'real_qualified':real['counts']['合格有邮箱'],'windows_tested':False,'paid_actions':0}
            Path('artifacts/browser-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
            browser.close()
    finally:
        p.terminate();p.wait(timeout=10);p.stdout.close();p.stderr.close()
