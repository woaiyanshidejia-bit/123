"""Extract release, verify manifest, install offline in fresh venv, run packaged tests."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
root=Path(__file__).resolve().parents[1];archive=root/'artifacts/YINGBA-Windows-v1.0.0.zip'
with tempfile.TemporaryDirectory(prefix='yingba-package-') as tmp:
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        forbidden=['/data/','/.git/','auth.json','/.env','__pycache__','.pyc']
        assert not any(bad in n for bad in forbidden for n in names)
        assert z.testzip() is None;z.extractall(tmp)
    app=Path(tmp)/'YINGBA';manifest=json.loads((app/'artifacts/MANIFEST.sha256.json').read_text())
    for name,digest in manifest.items():assert hashlib.sha256((app/name).read_bytes()).hexdigest()==digest,name
    subprocess.run([sys.executable,'-m','venv',str(Path(tmp)/'venv')],check=True,capture_output=True)
    python=Path(tmp)/'venv/bin/python'
    installed=subprocess.run([str(python),'-m','pip','install','--no-index','--find-links',str(app/'wheels'),'-r',str(app/'requirements.lock')],capture_output=True,text=True,check=True)
    tested=subprocess.run([str(python),'-m','unittest','discover','-s','tests','-v'],cwd=app,capture_output=True,text=True,check=True)
    result={'archive':archive.name,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'entries':len(names),'manifest_files_verified':len(manifest),'fresh_venv_offline_install':'PASS','packaged_unit_tests':'PASS','forbidden_runtime_or_auth_files':0,'windows_executed':False}
    (root/'artifacts/package-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    output=root/'artifacts/offline-install-output.txt'
    if not output.exists():output.write_text(installed.stdout+'\n'+tested.stderr,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
