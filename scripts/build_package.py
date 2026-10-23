"""Allowlist-only release packaging. Never includes runtime data or credentials."""
from pathlib import Path
import hashlib
import json
import zipfile
ROOT=Path(__file__).resolve().parents[1]
files=[]
for name in ('yingba','tests','scripts','docs','wheels','examples','artifacts'):
    for p in (ROOT/name).rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.zip') and p.name not in ('package-report.json','MANIFEST.sha256.json'):files.append(p)
files += [ROOT/'.gitignore',ROOT/'.gitattributes',ROOT/'README.md',ROOT/'requirements.lock',*(ROOT.glob('*.cmd'))]
manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
(ROOT/'artifacts'/'MANIFEST.sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
files.append(ROOT/'artifacts'/'MANIFEST.sha256.json')
output=ROOT/'artifacts'/'YINGBA-Windows-v1.0.0.zip'
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(set(files)):z.write(p,'YINGBA/'+str(p.relative_to(ROOT)))
print(output)
print(hashlib.sha256(output.read_bytes()).hexdigest())
