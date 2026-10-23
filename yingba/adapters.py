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
    """Subscription-only CLI bridge with explicit, persisted local acceptance."""
    def __init__(self, runner=None, *, receipt_path=None):
        from .codex_bridge import CodexProtocol
        self.protocol = CodexProtocol(runner=runner, receipt_path=receipt_path)
        self.runner = self.protocol.runner
    def status(self):
        return self.protocol.preflight()
    def preflight(self):
        return self.protocol.preflight()
    def validate_local(self, *, enabled=False):
        return self._call(self.protocol.validate_local, enabled=enabled)
    def evaluate(self, trusted_prompt, *, enabled=False):
        # Arbitrary prompts cannot bypass the company/evidence contract.
        if enabled is not True: raise ConnectionPause('订阅调用未获本机显式启用')
        raise ConnectionPause('请使用有固定 Schema 和证据绑定的 review_company；不执行任意提示词')
    def review_company(self, record, evidence, *, enabled=False):
        """Review already fetched evidence. Never fetches web pages or emails.

        ``evidence`` is a list of {id, url, text}; see the documented JSON schema.
        Local subscription acceptance must pass before any company review.
        """
        return self._call(self.protocol.review_company, record, evidence, enabled=enabled)
    def review_cost(self, action, evidence, *, enabled=False):
        """Interpret supplied cost evidence; never authorizes or executes action."""
        return self._call(self.protocol.review_cost, action, evidence, enabled=enabled)
    @staticmethod
    def _call(method, *args, **kwargs):
        from .codex_bridge import ProtocolPause
        try: return method(*args, **kwargs)
        except ProtocolPause as exc: raise ConnectionPause(str(exc)) from None
