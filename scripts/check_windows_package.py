"""Cloud-side package checks; these deliberately do not claim to execute cmd.exe."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.parser import Parser
from zipfile import ZipFile


LAUNCHERS = ("安装.cmd", "启动.cmd", "诊断.cmd", "登录Codex.cmd")
PATH_CASES = (
    "C:\\YINGBA\\",
    "C:\\客户资料\\YINGBA 工作台\\",
    "D:\\YINGBA ! 100% & OEM (2026)^\\",
    "\\\\server\\客户共享\\YINGBA 工作台 !\\",
)


class AuditError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_launchers(root: Path) -> dict:
    scripts = {}
    for filename in LAUNCHERS:
        blob = (root / filename).read_bytes()
        require(not blob.startswith(b"\xef\xbb\xbf"), f"{filename}: CMD must not have a BOM")
        require(b"\r\n" in blob and b"\n" not in blob.replace(b"\r\n", b""),
                f"{filename}: all lines must use CRLF")
        text = blob.decode("utf-8")
        lines = text.splitlines()
        guard = "setlocal EnableExtensions DisableDelayedExpansion"
        pushd = 'pushd "%~dp0" >nul 2>&1'
        require(guard in lines and pushd in lines, f"{filename}: guarded quoted pushd required")
        require(lines.index(guard) < lines.index(pushd), f"{filename}: disable delayed expansion before path use")
        require(lines[lines.index(pushd) + 1] == "if errorlevel 1 goto folder_failed",
                f"{filename}: pushd failure must stop work")
        require("chcp 65001 >nul" in lines, f"{filename}: UTF-8 console required")
        require(lines.index("chcp 65001 >nul") < next(i for i, s in enumerate(lines) if not s.isascii()),
                f"{filename}: select UTF-8 before Chinese output")
        require("popd" in lines and "exit /b %YINGBA_EXIT%" in lines,
                f"{filename}: restore directory and preserve exit status")
        require(not any(s.lower().startswith("cd ") for s in lines), f"{filename}: unchecked cd forbidden")
        # Validate only the quoted path expression, not a full Windows shell emulator.
        for path in PATH_CASES:
            rendered = pushd.replace("%~dp0", path)
            quoted = rendered[len('pushd "') : -len('" >nul 2>&1')]
            require(quoted == path and PureWindowsPath(quoted).is_absolute(),
                    f"{filename}: path substitution changed the path")
        scripts[filename] = text
    installer = scripts["安装.cmd"]
    for fragment in ("py -3.12 -c", "python -c", "sys.version_info[:2] == (3, 12)",
                     "struct.calcsize('P') == 8", "--isolated install", "--no-index", "--no-cache-dir",
                     "--force-reinstall", "--require-hashes", '--find-links "wheels"', '-r "requirements.lock"'):
        require(fragment in installer, f"installer missing {fragment}")
    for filename, command in (("启动.cmd", '".venv\\Scripts\\python.exe" -m yingba.server'),
                              ("诊断.cmd", '".venv\\Scripts\\python.exe" -m yingba.server --diagnose'),
                              ("登录Codex.cmd", "call codex -c forced_login_method=chatgpt login")):
        require(command + '\r\nset "YINGBA_EXIT=%ERRORLEVEL%"' in scripts[filename],
                f"{filename}: command status must be captured immediately")
    for variable in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
                     "AZURE_OPENAI_API_KEY", "CODEX_MODEL_PROVIDER", "CODEX_ACCESS_TOKEN"):
        require(f"if defined {variable} goto api_config_blocked" in scripts["登录Codex.cmd"],
                f"login missing forbidden environment guard: {variable}")
    return {"files": list(scripts), "encoding": "UTF-8 without BOM / CRLF",
            "path_cases_statically_checked": list(PATH_CASES), "cmd_executed": False}


def check_wheels(root: Path) -> list[dict]:
    pins = {}
    for line in (root / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([\w.-]+)==([\w.]+) --hash=sha256:([a-f0-9]{64})", line)
        require(match is not None, "every dependency must have an exact version and SHA256")
        name, version, digest = match.groups()
        name = normalized_name(name)
        require(name not in pins, f"duplicate dependency: {name}")
        pins[name] = (version, digest)
    require(bool(pins), "no pinned dependencies")
    found = {}
    for wheel in sorted((root / "wheels").glob("*.whl")):
        with ZipFile(wheel) as archive:
            metadata_files = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            wheel_files = [n for n in archive.namelist() if n.endswith(".dist-info/WHEEL")]
            require(len(metadata_files) == len(wheel_files) == 1, f"invalid wheel metadata: {wheel.name}")
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
            tags = Parser().parsestr(archive.read(wheel_files[0]).decode("utf-8")).get_all("Tag", [])
            name, version = normalized_name(metadata["Name"]), metadata["Version"]
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            require(name in pins and pins[name] == (version, digest), f"wheel hash or version mismatch: {wheel.name}")
            require(name not in found, f"duplicate wheel: {name}")
            require(bool(tags) and all(tag.endswith("-none-any") for tag in tags),
                    f"wheel requires platform-specific validation: {wheel.name}")
            dependencies = []
            for dep in metadata.get_all("Requires-Dist", []):
                # Current wheels have no environment markers or optional requirements.
                require(";" not in dep and "[" not in dep, f"dependency requires manual marker review: {dep}")
                dep_name = normalized_name(re.match(r"[\w.-]+", dep).group())
                require(dep_name in pins, f"missing offline dependency: {dep}")
                dependencies.append(dep)
            found[name] = {"filename": wheel.name, "version": version, "sha256": digest,
                           "tags": tags, "requires_dist": dependencies}
    require(set(found) == set(pins), "a locked wheel is missing from the package")
    return list(found.values())


def path_smoke(root: Path) -> dict:
    """Actual Linux subprocess/venv/XLSX/SQLite smoke in a difficult Unicode path."""
    with tempfile.TemporaryDirectory(prefix="yingba-windows-static-") as temp:
        work = Path(temp) / "中文工作台 空格 ! 100% & (OEM)^"
        work.mkdir()
        shutil.copy2(root / "requirements.lock", work)
        shutil.copytree(root / "wheels", work / "wheels")
        env = dict(os.environ)
        for variable in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(variable, None)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")

        def run(args: list[str]) -> subprocess.CompletedProcess:
            result = subprocess.run(args, cwd=work, env=env, capture_output=True, text=True,
                                    encoding="utf-8", timeout=90, check=False)
            require(result.returncode == 0, f"path smoke failed: {result.stdout}\n{result.stderr}")
            return result

        run([sys.executable, "-m", "venv", str(work / ".venv")])
        python = work / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installed = run([str(python), "-m", "pip", "--isolated", "install", "--no-index",
                         "--no-cache-dir", "--require-hashes", "--find-links", "wheels", "-r", "requirements.lock"])
        result = run([str(python), "-c", """
import json, sqlite3
from pathlib import Path
import openpyxl
p = Path('数据 中文 ! & %'); p.mkdir()
db = sqlite3.connect(p / '工作台.db')
db.execute('create table sample(name text)')
db.execute('insert into sample values (?)', ('润滑油 OEM 中文',)); db.commit()
assert db.execute('select name from sample').fetchone()[0] == '润滑油 OEM 中文'
db.close()
w = openpyxl.Workbook(); w.active.append(['公司名称', '邮箱']); w.active.append(['中文公司', 'sales@example.invalid'])
w.save(p / '模拟导出.xlsx')
r = openpyxl.load_workbook(p / '模拟导出.xlsx', read_only=True)
assert list(r.active.values)[1] == ('中文公司', 'sales@example.invalid'); r.close()
print(json.dumps({'openpyxl': openpyxl.__version__, 'sqlite': sqlite3.sqlite_version,
                  'unicode_sqlite_xlsx_roundtrip': True}, ensure_ascii=False))
"""])
        return {"executed_on": sys.platform, "windows_execution": os.name == "nt",
                "directory_name": work.name, "offline_install_exit": installed.returncode,
                "result": json.loads(result.stdout)}


def audit(root: Path, include_path_smoke: bool = True) -> dict:
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed",
              "scope": "Cloud static checks and optional host path smoke; not Windows acceptance",
              "launchers": check_launchers(root), "wheels": check_wheels(root),
              "windows_cmd_execution_verified": False, "browser_login_verified": False}
    if include_path_smoke:
        report["host_path_smoke"] = path_smoke(root)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--skip-path-smoke", action="store_true")
    args = parser.parse_args()
    report = audit(args.root, not args.skip_path_smoke)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
