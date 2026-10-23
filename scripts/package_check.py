"""Extract final ZIP, verify hashes, install offline and run its full tests."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import tempfile
import zipfile
root=Path(__file__).resolve().parents[1]
VERSION='1.2.0'
archive=root/'artifacts'/f'YINGBA-Windows-v{VERSION}.zip'
with tempfile.TemporaryDirectory(prefix='yingba-package-中文 空格 ! 100% & (OEM)^ ') as tmp:
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        forbidden=['/data/','/.git/','auth.json','codex-acceptance.json','/.env','__pycache__','.pyc']
        assert not any(bad in n for bad in forbidden for n in names)
        assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts
                   and not PureWindowsPath(n).drive and '\\' not in n
                   and PurePosixPath(n).parts[0]=='YINGBA' for n in names)
        assert all(not re.search(r'[<>:"|?*\x00-\x1f]',part) and not part.endswith((' ','.'))
                   and not re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part)
                   for n in names for part in PurePosixPath(n).parts)
        assert all(((info.external_attr>>16)&0o170000)!=0o120000 for info in z.infolist())
        assert len(names)==len({n.casefold() for n in names}) and z.testzip() is None
        z.extractall(tmp)
    app=Path(tmp)/'YINGBA'
    manifest=json.loads((app/'artifacts/MANIFEST.sha256.json').read_text(encoding='utf-8'))
    assert set(names)=={'YINGBA/'+name for name in manifest}|{'YINGBA/artifacts/MANIFEST.sha256.json'}
    for name,digest in manifest.items():assert hashlib.sha256((app/name).read_bytes()).hexdigest()==digest,name
    env=dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONNOUSERSITE='1')
    for variable in ('PYTHONPATH','PYTHONHOME'):env.pop(variable,None)
    subprocess.run([sys.executable,'-m','venv',str(Path(tmp)/'venv')],check=True,capture_output=True,env=env,timeout=120)
    python=Path(tmp)/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    installed=subprocess.run([str(python),'-m','pip','--isolated','install','--no-index','--no-cache-dir','--require-hashes','--find-links',str(app/'wheels'),'-r',str(app/'requirements.lock')],capture_output=True,text=True,encoding='utf-8',check=True,env=env,timeout=120)
    windows_checked=subprocess.run([str(python),'scripts/check_windows_package.py','--skip-path-smoke'],cwd=app,capture_output=True,text=True,encoding='utf-8',env=env,timeout=60)
    tested=subprocess.run([str(python),'-m','unittest','discover','-s','tests','-v'],cwd=app,capture_output=True,text=True,encoding='utf-8',env=env,timeout=300)
    (root/'artifacts'/f'offline-install-output-v{VERSION}.txt').write_text(installed.stdout+'\n'+windows_checked.stdout+'\n'+windows_checked.stderr+'\n'+tested.stdout+'\n'+tested.stderr,encoding='utf-8')
    windows_checked.check_returncode()
    tested.check_returncode()
    count=int(re.search(r'Ran (\d+) tests',tested.stderr).group(1))
    result={'archive':archive.name,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'archive_bytes':archive.stat().st_size,'entries':len(names),'manifest_files_verified':len(manifest),'fresh_venv_offline_install':'PASS','packaged_windows_static_check':'PASS','packaged_unit_tests':'PASS','test_count':count,'forbidden_runtime_or_auth_files':0,'host_platform':sys.platform,'special_path_directory':Path(tmp).name,'windows_executed':os.name=='nt','cmd_execution_verified':False}
    (root/'artifacts'/f'package-report-v{VERSION}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
