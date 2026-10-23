"""Conservative public-site verification; no model, Apollo, cookies or email probing.

Network use is explicit by callers. The shipped task engine keeps real search
unconnected. Tests inject fixtures and make zero public requests.
"""
from dataclasses import dataclass
import http.client
from html.parser import HTMLParser
import ipaddress
import re
import socket
import queue
import threading
import time
import ssl
from urllib.parse import urlsplit,urljoin
from .normalize import domain,name,email,source_emails
from .adapters import ConnectionPause

@dataclass(frozen=True)
class Page:
    url: str
    html: str

class SiteText(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.links=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='a':
            href=dict(attrs).get('href','')
            if isinstance(href,str):self.links.append(href)
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.skip:self.skip-=1
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)
    @property
    def text(self):return ' '.join(self.parts)

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address,port,timeout):
        super().__init__(host,port,timeout=timeout,context=ssl.create_default_context());self.address=address
    def connect(self):
        sock=socket.create_connection((self.address,self.port),self.timeout)
        try:self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except BaseException:sock.close();raise

class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self,host,address,port,timeout):super().__init__(host,port,timeout=timeout);self.address=address
    def connect(self):self.sock=socket.create_connection((self.address,self.port),self.timeout)

class PublicFetcher:
    MAX_BYTES=150000
    DNS_TIMEOUT=10
    FETCH_DEADLINE=30
    def __init__(self,resolver=socket.getaddrinfo,connection_factory=None,clock=time.monotonic):
        self.resolver=resolver;self.connection_factory=connection_factory;self.clock=clock
    def resolve(self,host,port,deadline):
        result=queue.Queue(maxsize=1)
        def lookup():
            try:result.put((True,self.resolver(host,port,type=socket.SOCK_STREAM)))
            except Exception:result.put((False,None))
        threading.Thread(target=lookup,daemon=True).start()
        remaining=min(self.DNS_TIMEOUT,deadline-self.clock())
        if remaining<=0:raise ConnectionPause('公开网页核实总时限已到')
        try:ok,records=result.get(timeout=remaining)
        except queue.Empty:raise ConnectionPause('公开网页DNS解析超时') from None
        if not ok:raise ConnectionPause('公开网页DNS解析失败')
        return records
    def fetch(self,url,expected_domain):
        deadline=self.clock()+self.FETCH_DEADLINE
        for _ in range(4):
            if self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
            parsed=urlsplit(url);host=parsed.hostname or '';root=domain(host)
            if parsed.scheme!='https' or root!=expected_domain or parsed.username or parsed.password or parsed.port not in (None,443):raise ConnectionPause('公开网页URL不在已确认公司域名或端口范围')
            port=parsed.port or (443 if parsed.scheme=='https' else 80)
            try:
                candidates={item[4][0] for item in self.resolve(host,port,deadline)}
                if not candidates or any(not ipaddress.ip_address(ip).is_global for ip in candidates):raise ConnectionPause('公开网页解析包含非公网地址，已阻断')
                address=sorted(candidates)[0]
                factory=self.connection_factory or (PinnedHTTPS if parsed.scheme=='https' else PinnedHTTP)
                remaining=deadline-self.clock()
                if remaining<=0:raise ConnectionPause('公开网页核实总时限已到')
                connection=factory(host,address,port,min(10,remaining/3))
                try:
                    path=parsed.path or '/'
                    if parsed.query:path+='?'+parsed.query
                    connection.request('GET',path,headers={'User-Agent':'YINGBA-CompanyVerifier/1.0','Accept':'text/html','Accept-Encoding':'identity','Connection':'close'})
                    if self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
                    response=connection.getresponse()
                    if response.status in (301,302,303,307,308):
                        location=response.getheader('Location')
                        if not location:raise ConnectionPause('网页重定向缺少位置')
                        url=urljoin(url,location);continue
                    if response.status in (401,403,429):raise ConnectionPause('网页拒绝访问或限流，不绕过限制')
                    if response.status!=200:raise ConnectionPause('公开网页暂不可用')
                    if response.getheader('Content-Encoding','identity').lower()!='identity':raise ConnectionPause('不支持的网页压缩响应，已停止')
                    content_type=response.getheader('Content-Type','')
                    if 'text/html' not in content_type.lower():raise ConnectionPause('来源不是HTML网页')
                    chunks=[];size=0
                    while True:
                        if self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
                        # read1 performs at most one socket read, so slow streams cannot
                        # indefinitely reset the total wall-clock deadline.
                        chunk=response.read1(min(8192,self.MAX_BYTES+1-size))
                        if not chunk:break
                        chunks.append(chunk);size+=len(chunk)
                        if size>self.MAX_BYTES:raise ConnectionPause('网页超过核实大小限制')
                    body=b''.join(chunks)
                    encoding='utf-8'
                    if 'charset=' in content_type.lower():encoding=content_type.lower().split('charset=',1)[1].split(';')[0].strip(' "\x27')
                    try:html=body.decode(encoding,errors='replace')
                    except LookupError:raise ConnectionPause('网页编码无法识别') from None
                    return Page(url,html)
                finally:connection.close()
            except ConnectionPause:raise
            except (OSError,ValueError,http.client.HTTPException):raise ConnectionPause('公开网页连接失败；未绕过限制') from None
        raise ConnectionPause('网页重定向次数超限')

class PublicVerifier:
    def __init__(self,fetcher=None):self.fetcher=fetcher or PublicFetcher()
    def verify(self,record):
        root=domain(record.get('domain'));company=name(record.get('name'))
        if not root or not company:raise ConnectionPause('缺少公司名称或域名，不能公开核实')
        home=self.fetcher.fetch('https://'+root+'/',root);pages=[home]
        parser=SiteText();parser.feed(home.html)
        links=[]
        for href in parser.links:
            candidate=urljoin(home.url,href);parsed=urlsplit(candidate)
            if domain(parsed.hostname)!=root or parsed.scheme!='https':continue
            if any(term in parsed.path.lower() for term in ('contact','about','company')) and candidate not in links:links.append(candidate)
        # No guessing contact paths or mailboxes; only follow links actually on the page.
        for link in links[:2]:pages.append(self.fetcher.fetch(link,root))
        texts=[]
        for page in pages:
            parser=SiteText();parser.feed(page.html);texts.append(parser.text)
        visible=' '.join(texts);lower=visible.casefold()
        identity=company in name(visible)
        def contains(term):return bool(re.search(r'\b'+re.escape(term)+r'\b',lower)) if term.isascii() else term in lower
        lubricant=any(contains(term) for term in ('lubricant','lubricants','lubrication','润滑油','润滑脂'))
        category='';matched=[]
        for label,terms in [('润滑油 OEM/ODM',('oem','odm','private label','contract manufacturing','定制','代工')),('润滑油进口商',('importer','importing','进口')),('润滑油经销商',('distributor','dealer','wholesale','经销','批发')),('润滑油相关渠道',('supplier','retailer','供应商','零售'))]:
            matched=[term for term in terms if contains(term)]
            if matched:category=label;break
        addresses=set();primary=home.url
        for page,text in zip(pages,texts):
            # Addresses may be in mailto attributes but never exclusively inside scripts.
            parser=SiteText();parser.feed(page.html)
            page_emails=source_emails(text+' '+' '.join(x[7:] for x in parser.links if x.lower().startswith('mailto:')))
            page_emails={a for a in page_emails if a.split('@')[1]==root}
            if page_emails and not addresses:primary=page.url
            addresses.update(page_emails)
        body='\n\n'.join(page.url+'\n'+text for page,text in zip(pages,texts))
        # Retain exact observed mailto addresses so the engine can independently match them.
        body+='\nObserved public mailto: '+' '.join(sorted(addresses))
        return {'identity_confirmed':identity,'business_confirmed':lubricant and bool(category),'category':category,'url':primary,
                'basis':f'规则核实：公司名称精确规范化匹配={identity}；润滑油业务词={lubricant}；渠道依据={matched}；同域公开页面='+', '.join(p.url for p in pages),
                'text':body,'emails':sorted(addresses),'simulation':False}
