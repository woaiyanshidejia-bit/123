"""Negative package fixtures catch regressions without pretending to run Windows."""
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_windows_package import AuditError, LAUNCHERS, check_launchers, check_wheels


ROOT = Path(__file__).resolve().parents[1]


class WindowsPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for filename in (*LAUNCHERS, "requirements.lock"):
            shutil.copy2(ROOT / filename, self.root)
        shutil.copytree(ROOT / "wheels", self.root / "wheels")

    def mutate(self, filename, old, new):
        path = self.root / filename
        path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))

    def test_launchers_static_unicode_spaces_unc_and_metacharacters(self):
        report = check_launchers(self.root)
        self.assertEqual(len(report["files"]), 4)
        self.assertEqual(len(report["path_cases_statically_checked"]), 4)
        self.assertFalse(report["cmd_executed"])

    def test_all_locked_wheels_match_hashes_and_are_platform_independent(self):
        report = check_wheels(self.root)
        self.assertEqual({r["version"] for r in report}, {"3.1.5", "2.0.0"})

    def test_rejects_lf_only_cmd(self):
        self.mutate("启动.cmd", "\r\n", "\n")
        with self.assertRaisesRegex(AuditError, "CRLF"):
            check_launchers(self.root)

    def test_rejects_bom(self):
        p = self.root / "诊断.cmd"; p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
        with self.assertRaisesRegex(AuditError, "BOM"):
            check_launchers(self.root)

    def test_rejects_unsafe_delayed_expansion(self):
        self.mutate("启动.cmd", "DisableDelayedExpansion", "EnableDelayedExpansion")
        with self.assertRaisesRegex(AuditError, "guarded quoted pushd"):
            check_launchers(self.root)

    def test_rejects_ignored_directory_failure(self):
        self.mutate("安装.cmd", "if errorlevel 1 goto folder_failed", "rem ignored directory error")
        with self.assertRaisesRegex(AuditError, "pushd failure"):
            check_launchers(self.root)

    def test_rejects_codex_cmd_without_call(self):
        self.mutate("登录Codex.cmd", "call codex ", "codex ")
        with self.assertRaisesRegex(AuditError, "status must be captured"):
            check_launchers(self.root)

    def test_rejects_unprotected_api_environment(self):
        self.mutate("登录Codex.cmd", "if defined OPENAI_API_KEY goto api_config_blocked", "rem missing guard")
        with self.assertRaisesRegex(AuditError, "environment guard"):
            check_launchers(self.root)

    def test_rejects_unpinned_offline_dependencies(self):
        self.mutate("requirements.lock", "openpyxl==3.1.5", "openpyxl>=3.1.5")
        with self.assertRaisesRegex(AuditError, "exact version"):
            check_wheels(self.root)

    def test_rejects_wheel_tampering(self):
        p = self.root / "wheels" / "openpyxl-3.1.5-py2.py3-none-any.whl"
        p.write_bytes(p.read_bytes() + b"unexpected")
        with self.assertRaisesRegex(AuditError, "hash or version mismatch"):
            check_wheels(self.root)

    def test_rejects_missing_transitive_wheel(self):
        (self.root / "wheels" / "et_xmlfile-2.0.0-py3-none-any.whl").unlink()
        with self.assertRaisesRegex(AuditError, "locked wheel is missing"):
            check_wheels(self.root)

    def test_rejects_online_installer(self):
        self.mutate("安装.cmd", "--no-index ", "")
        with self.assertRaisesRegex(AuditError, "--no-index"):
            check_launchers(self.root)


if __name__ == "__main__":
    unittest.main()
