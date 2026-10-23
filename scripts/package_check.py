"""Extract final ZIP, verify hashes, install offline and run its full tests."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import zipfile
root=Path(__file__).resolve().parents[1]
archive=root/'artifacts/YINGBA-Windows-v1.1.0.zip'
with tempfile.TemporaryDirectory(prefix='yingba-package-中文 ') as tmp:
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        forbidden=['/data/','/.git/','auth.json','codex-acceptance.json','/.env','__pycache__','.pyc']
        assert not any(bad in n for bad in forbidden for n in names)
        assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in names)
        assert len(names)==len(set(names)) and z.testzip() is None
        z.extractall(tmp)
    app=Path(tmp)/'YINGBA'
    manifest=json.loads((app/'artifacts/MANIFEST.sha256.json').read_text(encoding='utf-8'))
    for name,digest in manifest.items():assert hashlib.sha256((app/name).read_bytes()).hexdigest()==digest,name
    assert set(names)=={'YINGBA/'+name for name in manifest}|{'YINGBA/artifacts/MANIFEST.sha256.json'}
    env=dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONNOUSERSITE='1')
    subprocess.run([sys.executable,'-m','venv',str(Path(tmp)/'venv')],check=True,capture_output=True,env=env)
    python=Path(tmp)/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    installed=subprocess.run([str(python),'-m','pip','--isolated','install','--no-index','--require-hashes','--find-links',str(app/'wheels'),'-r',str(app/'requirements.lock')],capture_output=True,text=True,encoding='utf-8',check=True,env=env)
    tested=subprocess.run([str(python),'-m','unittest','discover','-s','tests','-v'],cwd=app,capture_output=True,text=True,encoding='utf-8',env=env)
    (root/'artifacts/offline-install-output-v1.1.0.txt').write_text(installed.stdout+'\n'+tested.stdout+'\n'+tested.stderr,encoding='utf-8')
    tested.check_returncode()
    count=int(re.search(r'Ran (\d+) tests',tested.stderr).group(1))
    result={'archive':archive.name,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'archive_bytes':archive.stat().st_size,'entries':len(names),'manifest_files_verified':len(manifest),'fresh_venv_offline_install':'PASS','packaged_unit_tests':'PASS','test_count':count,'forbidden_runtime_or_auth_files':0,'windows_executed':False}
    (root/'artifacts/package-report-v1.1.0.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
