import re
import unicodedata
import ipaddress
from urllib.parse import urlsplit

LOCAL = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
EMAIL_TOKEN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+",re.I|re.ASCII)

def domain(value):
    value = str(value or '').strip().lower()
    if not value or '\\' in value or any(ord(c)<33 for c in value): return ''
    try:
        parsed = urlsplit(value if '://' in value else '//'+value)
        if parsed.username is not None or parsed.password is not None:return ''
        if parsed.scheme and parsed.scheme not in ('https','http'):return ''
        parsed.port  # Reject malformed ports rather than normalizing them away.
        host = parsed.hostname or ''
        host = host.rstrip('.').encode('idna').decode()
    except (ValueError, UnicodeError): return ''
    host=host[4:] if host.startswith('www.') else host
    if len(host)>253 or '.' not in host:return ''
    labels=host.split('.')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels):return ''
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:return ''
    return host

def name(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value or '')).casefold() if c.isalnum())

def email(value):
    value = str(value or '').strip()
    if value[:7].lower()=='mailto:':value=value[7:]
    # This release supports ASCII mailboxes only. Unicode casefold (e.g. ß→ss,
    # ſ→s) must never manufacture a different SMTP local part.
    if not value.isascii():return ''
    value=value.lower()
    if len(value)>254 or value.count('@')!=1:return ''
    local,host=value.split('@')
    normalized=domain(host)
    # A mailbox domain is exact; www is not silently stripped here.
    if host!=normalized or len(local.encode('utf-8'))>64 or not re.fullmatch(LOCAL,local):return ''
    if not re.search(r'\.[a-z]{2,63}$',host):return ''
    return value

def source_emails(text):
    """Match whole source tokens, never accept a prefix of another address/domain."""
    text=str(text or '');addresses=set()
    for match in EMAIL_TOKEN.finditer(text):
        # A regex limited to ASCII must not accept a valid-looking tail/head of
        # a malformed or international mailbox token.
        before=text[match.start()-1:match.start()] if match.start() else ''
        after=text[match.end():match.end()+1]
        if (before and (before.isalnum() or before in '._@')) or (after and (after.isalnum() or after in '_@')):continue
        token=match.group()
        if token.endswith('..'):continue
        address=email(token.removesuffix('.'))
        if address:addresses.add(address)
    return addresses

def keys(record):
    return [(k,v) for k,v in [('domain',domain(record.get('domain'))),('apollo',str(record.get('apollo_id') or '').strip().casefold()),('name',name(record.get('name'))) ] if v]

def cell(value):
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(value if value is not None else ''))
    if len(value)>32700:value=value[:32650]+'【超长内容；完整邮箱见邮箱清单，原文见审核附件】'
    # Also handle control characters preceding a spreadsheet formula.
    return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
