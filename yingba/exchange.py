import hashlib
import json
import os
import tempfile
from pathlib import Path
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font,PatternFill
from .engine import stamp
from .normalize import keys,email,cell

DEFAULT_MAPPING={'name':'公司名称','domain':'域名','apollo_id':'Apollo ID','email':'邮箱'}
def import_history(store,path,mapping=None):
    """Read only, no writes to historical original. Ambiguous headers fail closed."""
    path=Path(path); mapping=mapping or DEFAULT_MAPPING
    if path.suffix.lower()!='.xlsx':raise ValueError('请先将历史表另存为xlsx（不覆盖原件）')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    wb=load_workbook(path,read_only=True,data_only=True)
    try:
        rows=wb.active.iter_rows(values_only=True); header=list(next(rows))
        if len(header)!=len(set(header)):raise ValueError('历史表含重复列名，请调整字段映射')
        indexes={k:header.index(v) for k,v in mapping.items() if v in header}
        if not any(k in indexes for k in ('name','domain','apollo_id','email')):raise ValueError('未找到可去重字段，请配置字段映射')
        count=0
        with store.transaction() as db:
            if db.execute('SELECT 1 FROM history_imports WHERE sha256=?',(digest,)).fetchone():return 0
            for row in rows:
                r={k:row[i] for k,i in indexes.items()}
                identities=keys(r)
                address=email(r.get('email'))
                if address:identities.append(('email',address))
                if not identities:continue
                for k,v in identities:db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,NULL,1)',(k,v))
                count+=1
            db.execute('INSERT INTO history_imports(source,sha256,imported,rows) VALUES(?,?,?,?)',(path.name,digest,stamp(),count))
        return count
    finally:wb.close()

def export(store,task,directory):
    """Single consistent read transaction; official sheets contain qualified only."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with store.connect() as db:
        db.execute('BEGIN')
        t=dict(db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone())
        rows=[dict(r) for r in db.execute("SELECT c.*,e.url,e.collected evidence_time,e.basis,e.body_hash, m.address FROM companies c JOIN emails m ON m.company=c.id JOIN evidence e ON e.id=m.evidence AND e.company=c.id WHERE c.task=? AND c.stage='qualified' ORDER BY c.id,m.address",(task,))]
        raw=[dict(r) for r in db.execute('SELECT * FROM raw_records WHERE task=?',(task,))]
        evidence=[dict(r) for r in db.execute('SELECT e.* FROM evidence e JOIN companies c ON e.company=c.id WHERE c.task=?',(task,))]
        excluded=[dict(r) for r in db.execute("SELECT id,name,domain,stage,reason FROM companies WHERE task=? AND stage!='qualified'",(task,))]
    grouped={}
    for r in rows:grouped.setdefault(r['id'],[]).append(r)
    wb=Workbook(); companies=wb.active;companies.title='公司总表'
    companies.append(['公司ID','公司名称','域名','Apollo ID','邮箱','来源URL','采集时间UTC','核验依据','状态'])
    for cid,rs in grouped.items():
        r=rs[0];companies.append([cid,cell(r['name']),cell(r['domain']),cell(r['apollo']),'; '.join(x['address'] for x in rs),r['url'],r['evidence_time'],cell(r['basis']),'模拟合格' if t['mode']=='simulation' else '公开核实；非投递验证'])
    sheet=wb.create_sheet('唯一邮箱清单');sheet.append(['邮箱','归属公司ID','公司名称','来源URL','采集时间UTC','核验依据'])
    by_address={}
    for r in rows:by_address.setdefault(r['address'],[]).append(r)
    for address,rs in by_address.items():sheet.append([cell(address),'; '.join(str(r['id']) for r in rs),cell('; '.join(r['name'] for r in rs)),rs[0]['url'],rs[0]['evidence_time'],cell(rs[0]['basis'])])
    meta=wb.create_sheet('导出说明');meta.append(['项目','值'])
    for k,v in {'模式':'模拟数据，禁止作为真实客户使用' if t['mode']=='simulation' else '真实数据','任务ID':task,'目标':t['target'],'合格公司':len(grouped),'唯一邮箱':len(by_address),'任务状态':t['status'],'说明':'公开来源核实不等于实际投递验证；不含邮件内容；未审核不计达标','导出UTC':stamp()}.items():meta.append([k,v])
    for ws in wb:
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for c in ws[1]:c.font=Font(color='FFFFFF',bold=True);c.fill=PatternFill('solid',fgColor='174A44')
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(65,max(14,max(len(str(c.value or '')) for c in col)+2))
    prefix='SIMULATION_' if t['mode']=='simulation' else 'YINGBA_'
    # Unique artifacts; no silent overwrite of earlier exports.
    token=__import__('uuid').uuid4().hex[:10]
    path=directory/f'{prefix}{task[:8]}_{token}.xlsx'
    wb.save(path)
    check=load_workbook(path,read_only=True)
    try:
        assert check['公司总表'].max_row-1==len(grouped)
        assert check['唯一邮箱清单'].max_row-1==len(by_address)
    finally:check.close()
    audit=path.with_suffix('.audit.json')
    audit.write_text(json.dumps({'task':t,'simulation':t['mode']=='simulation','raw_records':raw,'evidence':evidence,'excluded':excluded,'counts':{'companies':len(grouped),'unique_emails':len(by_address)},'xlsx_sha256':hashlib.sha256(path.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
    return path,audit
