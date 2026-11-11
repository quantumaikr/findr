"""the one-line app installers (get-app.sh, get-app.ps1): they parse, they check the download against SHA256SUMS.txt
before installing, and they never touch a security setting"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_get_app_sh_parses():
    subprocess.run(["sh", "-n", str(ROOT / "get-app.sh")], check=True)


def test_scripts_check_before_installing_and_leave_security_alone():
    sh = (ROOT / "get-app.sh").read_text(encoding="utf-8")
    ps = (ROOT / "get-app.ps1").read_bytes()
    assert ps.startswith(bytes.fromhex("efbbbf"))   # Windows PowerShell 5.1 reads a saved file without a BOM as the ANSI code page
    ps = ps.decode("utf-8-sig")
    for s in (sh, ps):
        assert "SHA256SUMS.txt" in s
    # the check comes before anything is installed or started
    assert sh.index("Check failed") < sh.index('mv "$TMP/findr.app"') and sh.index("Check failed") < sh.index("findr-app\"")
    assert ps.index("Check failed") < ps.index("Start-Process")
    for bad in ("xattr", "spctl", "Unblock-File", "Set-MpPreference", "ExecutionPolicy Bypass", "VerifiedAndReputablePolicyState"):
        assert bad not in sh and bad not in ps, bad


def test_release_publishes_checksums_and_macos_is_signed_ad_hoc():
    import json
    wf = (ROOT / ".github" / "workflows" / "desktop.yml").read_text(encoding="utf-8")
    assert "sha256sum * > SHA256SUMS.txt" in wf
    conf = json.loads((ROOT / "app" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["bundle"]["macOS"]["signingIdentity"] == "-"
