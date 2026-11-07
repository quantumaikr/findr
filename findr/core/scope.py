"""Which folders of a whole PC hold the person's own files (2026-11-07).
A person searches for what they made, received, opened or saved — not for what Windows or programs put there.
    user_roots()      -> [(order, label, path)]  the folders to index, most likely to be searched first
    skip_dir(rel, d)  -> True for system / program / cache / library folders (walked by the indexer)
Location is the main signal; file types and the indexer's own rules (tiers, secrets, libraries) do the rest."""
import os, re
from pathlib import Path

SYS_TOP = re.compile(r"^(windows|program files|program files \(x86\)|programdata|\$recycle\.bin|system volume information|recovery|perflogs|"
                     r"msocache|inetpub|msi|dell|hp|lenovo|asus|acer|samsung|lg|swsetup|oem|esd|xboxgames|windows\.old|onedrivetemp_|\$windows\.~ws|\$winreagent|\$sysreset|intel|amd|nvidia|drivers|onedrivetemp|config\.msi|users|documents and settings|onedrive|\$windows\.~bt)$", re.I)
JUNK_DIRS = {"appdata", "node_modules", "site-packages", "dist-packages", ".git", ".svn", ".hg", ".venv", "venv", "__pycache__", ".cache", "cache",
             "caches", "tmp", "temp", ".nuget", ".gradle", ".m2", ".cargo", ".rustup", ".npm", ".pnpm-store", ".idea", ".vscode", "build", "dist",
             "obj", "packages", "wpsystem", ".android", ".conda", "anaconda3", "miniconda3", "steamapps", "$recycle.bin"}
USER_DIRS = [  # (order, label, names) — order: what people look for first
    (1, "바탕화면", ("Desktop", "바탕 화면")),
    (1, "다운로드", ("Downloads", "다운로드")),
    (2, "문서", ("Documents", "문서")),
    (2, "클라우드", ("OneDrive", "Google Drive", "GoogleDrive", "Dropbox")),
    (3, "사진", ("Pictures", "사진")),
    (4, "동영상", ("Videos", "동영상")),
    (4, "음악", ("Music", "음악")),
]
SKIP_USERS = {"public", "default", "default user", "all users", "defaultappool", "wdagutilityaccount"}


def skip_dir(rel, d):
    """rel: path of the parent relative to its drive root ('/' for the top); d: the folder name"""
    n = d.lower()
    if rel in ("", "/") and SYS_TOP.match(n):
        return True
    return n in JUNK_DIRS or d.startswith("$") or d.startswith("~")


def _places(mnt):
    """(home folders to look in, drive tops) for this system: Windows, macOS, Linux, or Windows seen from WSL"""
    import string, sys
    if os.name == "nt":   # only the signed-in person's folders: on a shared PC other accounts are not ours to index
        drives = [Path(f"{d}:\\") for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
        return [Path.home()], drives
    if sys.platform == "darwin":
        return [Path.home()], []
    wsl = Path(mnt) / "c" / "Users"
    if wsl.is_dir():   # WSL: the Windows accounts and drives
        homes = []
        try:
            homes = [u for u in sorted(wsl.iterdir()) if u.name.lower() not in SKIP_USERS and u.is_dir()]
        except OSError:
            pass
        return homes, sorted(d for d in Path(mnt).iterdir() if len(d.name) == 1)
    return [Path.home()], []


def user_roots(mnt="/mnt"):
    """standard user folders, cloud folders, then non-system folders at the top of each drive"""
    out, seen = [], set()
    homes, drives = _places(mnt)
    for u in homes:
        for order, label, names in USER_DIRS:
            for nm in names:
                try:
                    cand = [u / nm] + ([u / "Documents" / nm] if label == "클라우드" else []) + sorted(u.glob(nm + " - *"))
                    for p in cand:
                        if p.is_dir() and p.resolve() not in seen:
                            seen.add(p.resolve()); out.append((order, label, p))
                except OSError:
                    continue
    for drv in drives:
        try:
            tops = sorted(drv.iterdir())
        except OSError:
            continue
        for t in tops:
            try:
                is_dir = t.is_dir()
            except OSError:
                continue
            if is_dir and not t.is_symlink() and not skip_dir("/", t.name) and t.resolve() not in seen:   # junctions point back into Users
                seen.add(t.resolve()); out.append((5, f"{(drv.name or drv.drive).rstrip(':').upper()}: 기타 폴더", t))
    out = sorted(out, key=lambda x: x[0])
    kept = []   # a folder inside another chosen folder is walked with it (no double scan)
    for o, lab, p in out:
        rp = p.resolve()
        if not any(rp != k.resolve() and str(rp).startswith(str(k.resolve()) + "/") for _, _, k in out):
            kept.append((o, lab, p))
    return kept


if __name__ == "__main__":
    import collections
    roots = user_roots()
    c = collections.Counter(label for _, label, _ in roots)
    print("roots by kind (counts only):", dict(c))
