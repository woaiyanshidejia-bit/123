"""Allowlist-only release packaging. Never includes runtime data or credentials."""
from pathlib import Path
import hashlib
import json
import zipfile
ROOT=Path(__file__).resolve().parents[1]
VERSION='1.2.0'
CURRENT_EVIDENCE={'test-output-v1.2.0.txt','browser-report.json','http-report.json','windows-static-report.json','audit-summary-v1.2.0.json'}
missing_evidence=sorted(name for name in CURRENT_EVIDENCE if not (ROOT/'artifacts'/name).is_file())
if missing_evidence:raise SystemExit('Cannot package before current evidence is ready: '+', '.join(missing_evidence))
files=[]
for name in ('yingba','tests','scripts','docs','wheels','examples','artifacts'):
    for p in (ROOT/name).rglob('*'):
        if name=='artifacts' and p.name not in CURRENT_EVIDENCE:continue
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.zip') and p.name not in ('package-report.json','MANIFEST.sha256.json'):
            if p.is_symlink():raise SystemExit('Refusing symlink in package: '+str(p.relative_to(ROOT)))
            files.append(p)
files += [ROOT/'.gitignore',ROOT/'.gitattributes',ROOT/'README.md',ROOT/'requirements.lock',*(ROOT.glob('*.cmd'))]
manifest={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
(ROOT/'artifacts'/'MANIFEST.sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
files.append(ROOT/'artifacts'/'MANIFEST.sha256.json')
output=ROOT/'artifacts'/f'YINGBA-Windows-v{VERSION}.zip'
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(set(files)):z.write(p,'YINGBA/'+p.relative_to(ROOT).as_posix())
print(output)
print(hashlib.sha256(output.read_bytes()).hexdigest())
