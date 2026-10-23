import re
import unicodedata
from urllib.parse import urlsplit

def domain(value):
    value = str(value or '').strip().lower()
    if not value: return ''
    try:
        host = urlsplit(value if '://' in value else '//'+value).hostname or ''
        host = host.rstrip('.').encode('idna').decode()
    except (ValueError, UnicodeError): return ''
    return host[4:] if host.startswith('www.') else host

def name(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value or '')).casefold() if c.isalnum())

def email(value):
    value = str(value or '').strip().removeprefix('mailto:').casefold()
    if not re.fullmatch(r'[a-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,63}',value): return ''
    return value

def keys(record):
    return [(k,v) for k,v in [('domain',domain(record.get('domain'))),('apollo',str(record.get('apollo_id') or '').strip().casefold()),('name',name(record.get('name'))) ] if v]

def cell(value):
    value = str(value or '')
    return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
