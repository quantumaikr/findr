"""Which folders of a whole PC hold the person's own files (2026-11-07).
A person searches for what they made, received, opened or saved — not for what Windows or programs put there.
    user_roots()      -> [(order, label, path)]  the folders to index, most likely to be searched first
    skip_dir(rel, d)  -> True for system / program / cache / library folders (walked by the indexer)
Location is the main signal; file types and the indexer's own rules (tiers, secrets, libraries) do the rest."""
import os, re, stat
from pathlib import Path

SYS_TOP = re.compile(r"^(windows|program files|program files \(x86\)|programdata|\$recycle\.bin|system volume information|recovery|perflogs|"
                     r"msocache|inetpub|msi|dell|hp|lenovo|asus|acer|samsung|lg|swsetup|oem|esd|xboxgames|windows\.old|onedrivetemp_|\$windows\.~ws|\$winreagent|\$sysreset|intel|amd|nvidia|drivers|onedrivetemp|config\.msi|users|documents and settings|onedrive|\$windows\.~bt"
                     r"|boot|system\.sav|efi|recovery image|"
                     # games and launchers: not the person's documents
                     r"nexon|steam|steamlibrary|riot games|epic games|blizzard|battle\.net|ea games|origin games|gog games|ubisoft|netmarble|smilegate|pearlabyss|kakaogames"
                     r")$", re.I)
OEM_DRIVERS = re.compile(r"(driver|드라이버|드라이브 설치)", re.I)   # 'hp드라이버', 'Dell Drivers', 'LenovoDrivers'
JUNK_DIRS = {"appdata", "node_modules", "site-packages", "dist-packages", ".git", ".svn", ".hg", ".venv", "venv", "__pycache__", ".cache", "cache",
             "caches", "tmp", "temp", ".nuget", ".gradle", ".m2", ".cargo", ".rustup", ".npm", ".pnpm-store", ".idea", ".vscode", "build", "dist",
             "obj", "packages", "wpsystem", ".android", ".conda", "anaconda3", "miniconda3", "steamapps", "$recycle.bin"}
USER_DIRS = [  # (order, label, names) — order: what people look for first
    (1, "바탕화면", ("Desktop", "바탕 화면")),
    (1, "다운로드", ("Downloads", "다운로드")),
    (2, "문서", ("Documents", "문서")),
    (2, "클라우드", ("OneDrive", "Google Drive", "GoogleDrive", "Dropbox", "iCloud Drive", "iCloudDrive")),
    (3, "사진", ("Pictures", "사진")),
    (4, "동영상", ("Videos", "동영상", "Movies")),   # Movies: macOS
    (4, "음악", ("Music", "음악")),
]
# children of a home folder that are not the person's own work (Windows, macOS, Linux)
HOME_SKIP = {"appdata", "library", "applications", "public", "contacts", "favorites", "links", "saved games", "searches", "3d objects",
             "intelgraphicsprofiles", "microsoftedgebackups", "ansel", "snap", "go", "miniconda3", "anaconda3", "miniforge3", "mambaforge",
             "micromamba", "scoop", "sync", "templates"}
SKIP_USERS = {"public", "default", "default user", "all users", "defaultappool", "wdagutilityaccount"}


# an embedded browser's profile (Chromium / CEF / Electron) that an app keeps inside a user folder, e.g. CapCut in ~/Movies:
# browser state, not the person's files. These files sit at the top of every such profile.
APP_PROFILE = {"SingletonLock", "SingletonSocket", "SingletonCookie", "Local State"}


def app_profile(files):
    """True when a folder's file names mark it as an app's browser profile"""
    return len(APP_PROFILE & set(files)) >= 2


def skip_dir(rel, d):
    """rel: path of the parent relative to its drive root ('/' for the top); d: the folder name"""
    n = d.lower()
    if rel in ("", "/") and (SYS_TOP.match(n) or OEM_DRIVERS.search(n)):
        return True
    return n in JUNK_DIRS or d.startswith("$") or d.startswith("~")


IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # a Windows junction (cloud placeholders such as OneDrive's carry other tags)


def is_link(p):
    """a symlink or a Windows junction. Path.is_symlink() misses junctions, and os.walk follows them: the legacy
    'Application Data' / 'Local Settings' in the home folder lead into AppData, 'Documents/My Music' back into Music"""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def _hidden_or_system(p):
    """Windows: hidden or system folders (Boot, recovery partitions' folders, vendor tools) are not the person's.
    lstat: a junction's own attributes, not those of the folder it leads to"""
    a = getattr(p.lstat(), "st_file_attributes", 0)
    return bool(a & (0x2 | 0x4))   # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def _fixed_drive(letter):
    """a local fixed disk (Windows GetDriveType == DRIVE_FIXED); network shares and removable drives are left out"""
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3
    except Exception:
        return Path(f"{letter}:\\").exists()


def _places(mnt):
    """(home folders to look in, drive tops) for this system: Windows, macOS, Linux, or Windows seen from WSL"""
    import string, sys
    if os.name == "nt":   # only the signed-in person's folders: on a shared PC other accounts are not ours to index
        drives = [Path(f"{d}:\\") for d in string.ascii_uppercase if _fixed_drive(d)]   # not network, USB or optical drives
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


def _inside(p, k):
    """p is k itself or inside it (any OS: compares path parts, not '/' text)"""
    try:
        Path(p).relative_to(k)
        return True
    except ValueError:
        return False


_FINDR_NAME = re.compile(r'^\s*name\s*=\s*["\']findr(-ai)?["\']\s*$', re.M)


def is_findr_checkout(d):
    """a copy of the findr repository, whichever one is running: findr/core/ next to a pyproject.toml whose project is findr
    (findr-ai). A second clone, an older install or a git worktree on the same PC is not the person's files either: its
    tests and docs hold the example office questions findr is tried with ('회의록', '사업계획서') and outranked the person's
    documents (#119)"""
    d = Path(d)
    try:
        if not (d / "findr" / "core").is_dir():
            return False
        with open(d / "pyproject.toml", encoding="utf-8", errors="replace") as f:
            return bool(_FINDR_NAME.search(f.read(4096)))
    except OSError:
        return False


def own_dirs():
    """findr's own folders: a source checkout (git clone in the home folder) and the data folder. Whole-PC search indexed
    findr's README and code, and they came up for questions about the person's files (#66)"""
    out = []
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").is_file() and (repo / "findr" / "core").is_dir():
        out.append(repo)
    for k in ("FINDR_HOME", "FINDR_DATA"):
        if os.environ.get(k):
            out.append(Path(os.environ[k]).expanduser().resolve())
    out.append((Path.home() / ".findr").resolve())
    return list(dict.fromkeys(out))


def user_roots(mnt="/mnt"):
    """standard user folders, cloud folders, other folders made in the home folder, then non-system folders at each drive top"""
    out, seen = [], set(own_dirs())
    homes, drives = _places(mnt)
    std = {nm.lower() for _, _, names in USER_DIRS for nm in names}
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
        # folders the person made directly in the home folder (projects, class material, 'source/repos', ...)
        try:
            kids = sorted(u.iterdir())
        except OSError:
            kids = []
        for p in kids:
            n = p.name.lower()
            try:
                ok = p.is_dir() and not is_link(p) and not n.startswith(".") and n not in std and n not in HOME_SKIP \
                    and not n.startswith(("onedrive", "google drive", "dropbox")) and not skip_dir("", p.name) and not _hidden_or_system(p)
            except OSError:
                ok = False
            if ok and p.resolve() not in seen and not is_findr_checkout(p):   # another copy of findr is not a root either (#119)
                seen.add(p.resolve()); out.append((5, "홈 폴더 기타", p))
    for drv in drives:
        try:
            tops = sorted(drv.iterdir())
        except OSError:
            continue
        for t in tops:
            try:
                ok = t.is_dir() and not is_link(t) and not skip_dir("/", t.name) and not _hidden_or_system(t)   # junctions point back into Users
            except OSError:
                continue
            if ok and t.resolve() not in seen and not is_findr_checkout(t):
                seen.add(t.resolve()); out.append((6, f"{(drv.name or drv.drive).rstrip(':').rstrip(chr(92)).upper()}: 기타 폴더", t))
    out = sorted(out, key=lambda x: x[0])
    kept = []   # a folder inside another chosen folder is walked with it (no double scan)
    for o, lab, p in out:
        rp = p.resolve()
        if not any(rp != k.resolve() and _inside(rp, k.resolve()) for _, _, k in out):
            kept.append((o, lab, p))
    return kept


if __name__ == "__main__":
    import collections
    roots = user_roots()
    c = collections.Counter(label for _, label, _ in roots)
    print("roots by kind (counts only):", dict(c))
