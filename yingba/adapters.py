"""No paid Apollo calls are implemented or made by this release."""
import os
import shutil
import subprocess
import tempfile
import json
from typing import Protocol

class ConnectionPause(Exception): pass
class TransientError(Exception): pass
class UnknownOutcome(ConnectionPause): pass
class Source(Protocol):
    def search(self,page:int)->tuple[list[dict],bool]: ...
    def verify(self,record:dict)->dict: ...

class Disconnected:
    def search(self,page): raise ConnectionPause('Apollo 未连接：未验证本地授权、接口及计费，禁止采集')
    def verify(self,record): raise ConnectionPause('公开来源核实适配器未验收')

class MockSource:
    """Synthetic .example identities, never real leads. 20 unique plus repeats."""
    def search(self,page):
        if page>3: return [],True
        indices={1:range(1,9),2:range(9,17),3:range(17,21)}[page]
        records=[{'name':f'模拟润滑油公司 {i:02}', 'domain':f'lube{i}.example','apollo_id':f'mock-{i}','mock_id':i} for i in indices]
        if page==2:
            records += [{'name':'同一公司别名','domain':'https://WWW.lube1.example/','apollo_id':'other','mock_id':1},dict(records[0])]
        return records,page==3
    def verify(self,record):
        i=record['mock_id']
        return {'identity_confirmed':i!=4, 'business_confirmed':i!=5,
                'category':'润滑油 OEM/ODM' if i%2 else '润滑油经销商',
                'url':f'https://lube{i}.example/contact',
                'basis':'模拟官网主体与润滑油业务信息，商务联系邮箱出现在公司联系页面',
                'emails':[] if i in (2,3) else [f'sales@lube{i}.example',f'SALES@LUBE{i}.EXAMPLE'],
                'text':f'SIMULATION company={i} sales@lube{i}.example', 'simulation':True}

class CodexSubscription:
    """Explicit opt-in CLI bridge; never loads or copies authentication files."""
    def __init__(self,runner=subprocess.run): self.runner=runner
    def status(self):
        if not shutil.which('codex'): return {'connected':False,'reason':'未安装 Codex CLI'}
        try:
            p=self.runner(['codex','login','status'],capture_output=True,text=True,timeout=15)
            # Do not expose stdout/stderr, which can contain credential metadata.
            ok=p.returncode==0 and 'chatgpt' in (p.stdout+p.stderr).lower()
            return {'connected':ok,'reason':'CLI报告ChatGPT登录；实际调用尚待本机验收' if ok else '未确认ChatGPT订阅登录，禁止API回退'}
        except (OSError,subprocess.TimeoutExpired): return {'connected':False,'reason':'CLI诊断失败'}
    def evaluate(self, trusted_prompt, *, enabled=False):
        if not enabled: raise ConnectionPause('订阅调用未获本机显式启用')
        if any(os.environ.get(k) for k in ('OPENAI_API_KEY','CODEX_API_KEY','OPENAI_BASE_URL','CODEX_ACCESS_TOKEN')):
            raise ConnectionPause('检测到API环境配置，拒绝调用')
        if not self.status()['connected']: raise ConnectionPause('未确认ChatGPT订阅登录')
        # Forced built-in provider/auth. No network tools, shell execution, or external MCP.
        with tempfile.TemporaryDirectory(prefix='yingba-codex-') as directory:
            # Version-gate supported isolation flags; do not silently drop them.
            help_result=self.runner(['codex','exec','--help'],capture_output=True,text=True,timeout=15)
            if help_result.returncode or '--ignore-user-config' not in help_result.stdout:
                raise ConnectionPause('CLI版本未提供所需配置隔离，请按指南升级后验收')
            args=['codex','exec','--ignore-user-config','--ephemeral','--skip-git-repo-check','--sandbox','read-only',
                  '-c','model_provider="openai"','-c','forced_login_method="chatgpt"',
                  '-c','features.shell_tool=false','--json','-']
            try:
                p=self.runner(args,input=trusted_prompt,cwd=directory,capture_output=True,text=True,timeout=180)
            except subprocess.TimeoutExpired as exc: raise ConnectionPause('Codex超时，需人工确认，不自动重试') from exc
            if p.returncode: raise ConnectionPause('Codex调用失败，请运行诊断；未使用API回退')
            return p.stdout
