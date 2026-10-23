"""Bounded public-site retrieval and conservative evidence extraction.

Rules provide a preliminary assessment only. Real qualification additionally
requires the subscription reviewer; remote HTML is always untrusted data.
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
import unicodedata
from urllib.parse import urlsplit,urljoin,unquote,urldefrag
from .normalize import domain,name,email,source_emails
from .adapters import ConnectionPause

@dataclass(frozen=True)
class Page:
    url: str
    html: str

class SiteText(HTMLParser):
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    BLOCK={'address','article','aside','blockquote','br','div','footer','h1','h2','h3','h4','header','li','main','nav','p','section','td','title','tr'}
    OMIT={'script','style','noscript','template','iframe','object','svg'}
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.links=[];self.stack=[];self.heading_parts=[]
    @property
    def hidden(self):return any(item[1] for item in self.stack)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);style=re.sub(r'\s+','',attrs.get('style') or '').casefold()
        hidden=(self.hidden or tag in self.OMIT or 'hidden' in attrs or (attrs.get('aria-hidden') or '').casefold()=='true'
                or any(value in style for value in ('display:none','visibility:hidden','opacity:0','font-size:0')))
        if not hidden and tag in self.BLOCK:self.parts.append('\n')
        if tag not in self.VOID:self.stack.append((tag,hidden))
        if not hidden and tag=='a':
            href=attrs.get('href','')
            if isinstance(href,str):self.links.append(href)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        if not self.hidden and tag in self.BLOCK:self.parts.append('\n')
        if tag in ('h1','h2','title'):self.heading_parts.append('\n')
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:];break
    def handle_data(self,data):
        if not self.hidden:
            self.parts.append(data)
            if any(item[0] in ('h1','h2','title') for item in self.stack):self.heading_parts.append(data)
    @property
    def text(self):return '\n'.join(' '.join(line.split()) for line in ''.join(self.parts).splitlines() if line.strip())
    @property
    def headings(self):return [' '.join(line.split()) for line in ''.join(self.heading_parts).splitlines() if line.strip()]

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
            try:
                if not isinstance(url,str) or '\\' in url or any(ord(c)<33 for c in url):raise ValueError('invalid URL')
                parsed=urlsplit(url);host=parsed.hostname or '';root=domain(host)
                if parsed.scheme!='https' or not root or root!=expected_domain or parsed.username is not None or parsed.password is not None or parsed.port not in (None,443):raise ConnectionPause('公开网页URL不在已确认公司域名或端口范围')
                host=host.rstrip('.').encode('idna').decode('ascii');port=parsed.port or 443
                candidates={item[4][0] for item in self.resolve(host,port,deadline)}
                def public(ip):
                    address=ipaddress.ip_address(ip)
                    return address.is_global and not address.is_multicast and not address.is_reserved and not address.is_unspecified
                if not candidates or any(not public(ip) for ip in candidates):raise ConnectionPause('公开网页解析包含非公网地址，已阻断')
                address=sorted(candidates)[0]
                factory=self.connection_factory or (PinnedHTTPS if parsed.scheme=='https' else PinnedHTTP)
                remaining=deadline-self.clock()
                if remaining<=0:raise ConnectionPause('公开网页核实总时限已到')
                connection=factory(host,address,port,min(10,remaining/3))
                expired=threading.Event()
                def expire(current=connection,signal=expired):
                    # getresponse() may internally read a header one byte at a
                    # time. Per-socket timeouts alone do not stop slowloris.
                    signal.set()
                    sock=getattr(current,'sock',None)
                    if sock is not None:
                        try:sock.shutdown(socket.SHUT_RDWR)
                        except (OSError,ValueError):pass
                    try:current.close()
                    except OSError:pass
                watchdog=threading.Timer(remaining,expire);watchdog.daemon=True;watchdog.start()
                try:
                    path=parsed.path or '/'
                    if parsed.query:path+='?'+parsed.query
                    connection.request('GET',path,headers={'User-Agent':'YINGBA-CompanyVerifier/1.0','Accept':'text/html','Accept-Encoding':'identity','Connection':'close'})
                    if self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
                    response=connection.getresponse()
                    if expired.is_set() or self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
                    if response.status in (301,302,303,307,308):
                        location=response.getheader('Location')
                        if not location:raise ConnectionPause('网页重定向缺少位置')
                        url=urljoin(url,location);continue
                    if response.status in (401,403,429):raise ConnectionPause('网页拒绝访问或限流，不绕过限制')
                    if response.status!=200:raise ConnectionPause('公开网页暂不可用')
                    if response.getheader('Content-Encoding','identity').lower()!='identity':raise ConnectionPause('不支持的网页压缩响应，已停止')
                    content_type=response.getheader('Content-Type','')
                    if content_type.split(';',1)[0].strip().lower()!='text/html':raise ConnectionPause('来源不是HTML网页')
                    chunks=[];size=0
                    while True:
                        if self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
                        # read1 performs at most one socket read, so slow streams cannot
                        # indefinitely reset the total wall-clock deadline.
                        chunk=response.read1(min(8192,self.MAX_BYTES+1-size))
                        if expired.is_set() or self.clock()>=deadline:raise ConnectionPause('公开网页核实总时限已到')
                        if not chunk:break
                        chunks.append(chunk);size+=len(chunk)
                        if size>self.MAX_BYTES:raise ConnectionPause('网页超过核实大小限制')
                    body=b''.join(chunks)
                    encoding='utf-8'
                    if 'charset=' in content_type.lower():encoding=content_type.lower().split('charset=',1)[1].split(';')[0].strip(' "\x27')
                    try:html=body.decode(encoding,errors='replace')
                    except LookupError:raise ConnectionPause('网页编码无法识别') from None
                    return Page(url,html)
                finally:watchdog.cancel();connection.close()
            except ConnectionPause:raise
            except (OSError,ValueError,UnicodeError,http.client.HTTPException):raise ConnectionPause('公开网页连接失败或URL无效；未绕过限制') from None
        raise ConnectionPause('网页重定向次数超限')

class PublicVerifier:
    CATEGORIES=[('润滑油 OEM/ODM',('oem','odm','private label','contract manufacturing','定制','代工')),
                ('润滑油进口商',('importer','importing','进口')),
                ('润滑油经销商',('distributor','distributors','dealer','wholesale','经销','批发')),
                ('润滑油相关渠道',('supplier','retailer','供应商','零售'))]
    NON_BUSINESS_MAILBOXES={'abuse','privacy','legal','security','dpo','noreply','no-reply','donotreply',
                           'do-not-reply','postmaster','webmaster','unsubscribe','careers','career','hr','jobs','recruitment'}
    def __init__(self,fetcher=None):self.fetcher=fetcher or PublicFetcher()
    @staticmethod
    def contains(text,term):
        text=text.casefold()
        return bool(re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',text)) if term.isascii() else term in text
    @staticmethod
    def identity_match(company,text):
        def words(value):return ' '.join(re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',value).casefold()))
        company=words(company);text=words(text)
        return bool(company and re.search(r'(?<!\w)'+re.escape(company)+r'(?!\w)',text))
    def verify(self,record):
        root=domain(record.get('domain'));company=record.get('name','')
        if not root or not isinstance(company,str) or not name(company):raise ConnectionPause('缺少公司名称或域名，不能公开核实')
        home=self.fetcher.fetch('https://'+root+'/',root);pages=[home]
        parser=SiteText();parser.feed(home.html)
        links=[]
        for href in parser.links:
            try:
                candidate=urldefrag(urljoin(home.url,href))[0];parsed=urlsplit(candidate)
                if domain(candidate)!=root or parsed.scheme!='https' or parsed.port not in (None,443) or parsed.query:continue
                if any(term in parsed.path.lower() for term in ('contact','about','company')) and candidate!=home.url and candidate not in links:links.append(candidate)
            except ValueError:continue
        # Follow only bounded, public links; never execute HTML/JS or guess emails.
        for link in links[:2]:pages.append(self.fetcher.fetch(link,root))
        observations=[];identity_quotes=[];business_quotes=[];category='';addresses=set();primary=home.url
        for page in pages:
            parser=SiteText();parser.feed(page.html);text=parser.text
            headings=parser.headings
            bad_context=('customer','client','partner','case study','news','vacancy','career','hiring','招聘','新闻','客户案例','合作伙伴')
            for heading in headings:
                if self.identity_match(company,heading) and not any(self.contains(heading,term) for term in bad_context):
                    identity_quotes.append({'url':page.url,'quote':heading})
            bad_page=any(term in urlsplit(page.url).path.casefold() for term in ('/news','/blog','/career','/jobs','/press'))
            if not bad_page:
                for block in text.splitlines():
                    if len(block)>1600:continue
                    if any(self.contains(block,term) for term in bad_context+('not','never','no longer','formerly','ceased','不是','不再','并非')):continue
                    lubricant=any(self.contains(block,term) for term in ('lubricant','lubricants','lubrication','润滑油','润滑脂'))
                    if not lubricant:continue
                    for label,terms in self.CATEGORIES:
                        if any(self.contains(block,term) for term in terms):
                            business_quotes.append({'url':page.url,'quote':block,'category':label})
                            if not category:category=label
                            break
            mailto=[]
            for href in parser.links:
                if href.casefold().startswith('mailto:'):
                    # Only URI recipients are evidence. Query subject/body may
                    # contain arbitrary third-party text and are never contacts.
                    path=unquote(urlsplit(href).path)
                    if '\r' not in path and '\n' not in path:mailto.append(path)
            body=text+('\n公开 mailto 收件人原文：'+' ; '.join(mailto) if mailto else '')
            page_emails={a for a in source_emails(body) if a.split('@')[1]==root and a.split('@')[0].split('+',1)[0] not in self.NON_BUSINESS_MAILBOXES}
            # This extraction is a candidate list; the structured reviewer must
            # additionally confirm business purpose and exact page quotations.
            if page_emails and not addresses:primary=page.url
            addresses.update(page_emails)
            observations.append({'url':page.url,'text':body,'emails':sorted(page_emails),
                                 'basis':'公司同域公开网页原文；邮箱仅提取可见文本及公开 mailto 收件人，等待订阅审核'})
        identity=bool(identity_quotes);business=identity and bool(business_quotes)
        body='\n\n'.join(page['url']+'\n'+page['text'] for page in observations)
        return {'identity_confirmed':identity,'business_confirmed':business,'category':category,'url':primary,
                'basis':f'规则初筛：主体标题依据={identity}；同句业务依据={bool(business_quotes)}；需订阅审核；公开页面='+', '.join(p.url for p in pages),
                'text':body,'emails':sorted(addresses),'simulation':False,'review_required':True,
                'evidence_pages':observations,'identity_quotes':identity_quotes,'business_quotes':business_quotes}
