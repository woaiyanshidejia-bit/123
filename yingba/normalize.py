import re
import unicodedata
from urllib.parse import urlsplit

LOCAL = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
EMAIL_TOKEN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+",re.I)

def domain(value):
    value = str(value or '').strip().lower()
    if not value: return ''
    try:
        host = urlsplit(value if '://' in value else '//'+value).hostname or ''
        host = host.rstrip('.').encode('idna').decode()
    except (ValueError, UnicodeError): return ''
    host=host[4:] if host.startswith('www.') else host
    if len(host)>253 or '.' not in host:return ''
    labels=host.split('.')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels):return ''
    return host

def name(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value or '')).casefold() if c.isalnum())

def email(value):
    value = str(value or '').strip().removeprefix('mailto:').casefold()
    if len(value)>254 or value.count('@')!=1:return ''
    local,host=value.split('@')
    normalized=domain(host)
    # A mailbox domain is exact; www is not silently stripped here.
    if host!=normalized or len(local.encode('utf-8'))>64 or not re.fullmatch(LOCAL,local):return ''
    if not re.search(r'\.[a-z]{2,63}$',host):return ''
    return value

def source_emails(text):
    """Match whole source tokens, never accept a prefix of another address/domain."""
    return {a for token in EMAIL_TOKEN.findall(str(text or '')) if (a:=email(token.rstrip('.')))}

def keys(record):
    return [(k,v) for k,v in [('domain',domain(record.get('domain'))),('apollo',str(record.get('apollo_id') or '').strip().casefold()),('name',name(record.get('name'))) ] if v]

def cell(value):
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(value or ''))
    # Also handle control characters preceding a spreadsheet formula.
    return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
