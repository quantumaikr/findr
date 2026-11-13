"""The Windows installer uses findr's copy of Tauri's NSIS template (app/src-tauri/windows/installer.nsi).

It must stay the template of the Tauri CLI the app is built with, and keep findr's two changes: upgrading does not
uninstall the old version by default (its uninstaller can delete the whole data folder), and the desktop shortcut
starts unticked.
"""
import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TEMPLATE = APP / "src-tauri" / "windows" / "installer.nsi"


def test_the_app_builds_with_this_template():
    conf = json.loads((APP / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["bundle"]["windows"]["nsis"]["template"] == "windows/installer.nsi"


def test_template_is_from_the_cli_the_app_is_built_with():
    lock = json.loads((APP / "package-lock.json").read_text(encoding="utf-8"))
    cli = lock["packages"]["node_modules/@tauri-apps/cli"]["version"]
    head = TEMPLATE.read_text(encoding="utf-8").splitlines()[0]
    m = re.search(r"@tauri-apps/cli (\d+\.\d+\.\d+)", head)
    assert m and m.group(1) == cli, (
        f"@tauri-apps/cli is {cli}: take its NSIS template and apply the findr changes again (see the top of the file)")


def test_upgrade_keeps_the_old_install_by_default():
    t = TEMPLATE.read_text(encoding="utf-8")
    i = t.index("Function PageReinstall\n")
    body = t[i:t.index("FunctionEnd", i)]
    pick = body.index("${AndIf} $R0 = 1\n      StrCpy $ReinstallPageCheck 2")
    assert pick < body.index("${If} $ReinstallPageCheck <> 2")   # decided before the radio buttons are checked


def test_desktop_shortcut_starts_unticked():
    t = TEMPLATE.read_text(encoding="utf-8")
    assert "!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED" in t
    assert t.index("!define MUI_FINISHPAGE_SHOWREADME\n") < t.index("!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED")


def test_no_control_characters():
    # a form feed in a comment (a \f that became one) ends the comment for makensis: the rest of the line was read as a
    # label outside any function and the Windows build stopped (0.2.22)
    for f in (TEMPLATE, TEMPLATE.with_name("hooks.nsh")):
        bad = [i for i, c in enumerate(f.read_bytes()) if c < 32 and c not in (9, 10, 13)]
        assert not bad, f"{f.name}: control character at byte {bad[0]}"
