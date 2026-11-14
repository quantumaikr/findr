"""findr index (2026-11-03): natural-language search over every file under a root, all local.

Tiers (by what a person looks for):
  1 full text  : the user's own documents and code (pdf, md, txt, html, py, json, logs ...) and application code
  2 head only  : bundled libraries (site-packages, python_embeded ...): first ~1500 chars, one chunk
  3 name only  : media, models, binaries, archives: path, name, size, date
Secrets (.env, keys, credentials) are listed by name only when allowed, never read.
    python index.py --root FOLDER --db INDEX.db
Re-running only re-reads files whose size or mtime changed.
"""
import argparse, datetime, unicodedata
import json
import os
import re
import sqlite3
import stat as _stat
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

if os.environ.get("FINDR_VERBOSE") != "1":   # the indexer is its own process: quiet as the CLI is ('Redirects are currently not supported', #67)
    import logging
    logging.getLogger("torch.distributed.elastic").setLevel(logging.ERROR)

TEXT = {".md", ".txt", ".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".csv", ".tsv", ".log", ".html", ".htm", ".css", ".sh", ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp",
        ".cu", ".cuh", ".java", ".go", ".rs", ".sql", ".xml", ".rst", ".ipynb", ".jinja", ".tex", ".r", ".m", ".lua", ""}
DOC = {".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".hwp", ".doc", ".ppt", ".xls", ".key",
       ".xlsb", ".ods", ".rtf", ".eml", ".emlx", ".mbox", ".pages", ".numbers"}   # moretext.py (sheets, RTF, e-mail), keynote_text (iWork)
AUD = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".wma", ".ogg"}   # transcribed locally by stt_cache.py (faster-whisper)
OLD = {".hwp", ".doc", ".ppt", ".xls"}   # old binary formats: converted locally by LibreOffice (+ H2Orestart for hwp), cached
SECRET = re.compile(r"^(\.env(\..*)?|\.envrc|\.netrc|_netrc|\.pgpass|pgpass\.conf|\.npmrc|\.pypirc|\.git-credentials|kubeconfig(\.ya?ml)?"
                    r"|id_(rsa|dsa|ecdsa|ed25519).*|credentials(\.json|\.ya?ml|\.txt|\.csv)?|.*_credentials\.csv|.*_accesskeys\.csv"
                    r"|secrets?\.(json|ya?ml|toml|txt|env)|.*\.(pem|pfx|p12|kdbx|kdb|keystore|jks|ppk|1pux|1pif)"
                    r"|\.(bash|zsh|sh|python|psql|mysql|node_repl|lesshst)_?history|\.history|ConsoleHost_history\.txt"   # shell histories hold typed tokens
                    r"|(sign|km)(Cert\.der|Pri\.key)"   # a Korean joint certificate (공동인증서): the certificate and its private key
                    # browsers' saved passwords: their CSV exports (Chrome / Edge / Safari 'Passwords.csv', 'Chrome 비밀번호.csv',
                    # Firefox 'logins.csv') and the stores themselves (Firefox logins.json + key4.db, Chrome 'Login Data'). Other CSV
                    # names with password / 비밀번호 in them are secret only when their header row is a credential export (CRED_CSV)
                    r"|passwords.*\.csv|(password|비밀번호)( ?\(\d+\))?\.csv|((microsoft )?edge|chrome|safari|firefox|brave|opera|vivaldi|whale|네이버 웨일|웨일|"
                    r"google|구글) ?(passwords?|비밀번호)( ?\(\d+\))?\.csv"
                    r"|logins( ?\(\d+\))?\.csv|logins\.json|key[34]\.db|signons\.sqlite|Login Data( For Account)?"
                    # password-manager exports: the manager's name and export / vault / backup in the name
                    # (bitwarden_export_….json, lastpass_vault_export.csv, 1PasswordExport-….csv, dashlane-credential-export.csv)
                    r"|(?=.*(bitwarden|lastpass|1password|keepass|dashlane|nordpass|roboform|enpass|proton ?pass))"
                    r"(?=.*(export|vault|backup)).*\.(csv|json|xml|txt|zip|pgp))$", re.I)
# folders of certificates, and any path naming them, are never read either (see secret_path): the NPKI / GPKI / EPKI folders
# that Korean banking, government and school sign-ins keep (with signCert.der, signPri.key), and copies made by hand
CERT_DIRS = {"npki", "gpki", "epki"}
# a folder named with these words (a certificate backup made by hand) hides the certificate files in it, not its other files
# ('공동인증서 발급 안내.pdf' and a guide in such a folder are ordinary documents)
CERT_WORDS = ("공동인증서", "공인인증서")
CERT_EXTS = {".der", ".key", ".pfx", ".p12", ".cer", ".crt", ".pem"}
# a CSV whose name says password / 비밀번호 but is not one of the known export names: secret when its header row is a
# credential export (a password column with a user name or site column); 'password_policy_review.csv' with other columns is read
CRED_NAME = re.compile(r"(password|비밀번호|passwd).*\.csv$", re.I)
# column names compared without spaces, '_' and '-' ('User Name', 'login_username', 'e-mail')
# (a whole cell: 'pw' and 'pwd' only as the column's whole name, case aside: 'PW', 'Pwd')
_CRED_PW = {"password", "passwd", "pwd", "pw", "pass", "passcode", "passphrase", "loginpassword", "비밀번호", "비번", "암호",
            "패스워드", "secret"}
_CRED_ID = {"username", "user", "userid", "login", "loginusername", "loginname", "email", "mail", "account", "id", "아이디", "사용자",
            "사용자이름", "사용자명", "계정", "url", "uri", "loginuri", "website", "site", "origin", "originurl", "hostname", "host",
            "domain", "사이트", "웹사이트", "주소", "도메인", "이메일", "name", "title"}
_CRED_PW_IN = ("password", "passwd", "비밀번호", "패스워드", "비번")   # inside a longer column name ('Password (encrypted)')
_CRED_ID_IN = ("username", "login", "아이디", "url")
# kinds of file whose first line is looked at: a password export saved as .txt or .tsv is one too
CRED_EXTS = {".csv", ".tsv", ".txt"}
# copy markers people's systems add to a name: 'Copy of x', 'x 2', 'x (1)', 'x copy', 'x - Copy', 'x - 복사본', 'x 복사본'
_COPY_PRE = re.compile(r"^(?:copy of |사본 - |복사본 - )+", re.I)
_COPY_SUF = re.compile(r"(?:\s*-\s*(?:복사본|사본|copy)(?:\s*\(\d+\))?|\s+(?:복사본|사본|copy)(?:\s+\d{1,3})?|\s*\(\d{1,3}\)|\s+\d{1,3})$", re.I)
_SECOND_EXT = re.compile(r"^(.+\.[^.]+)\.(txt|text|bak|old|orig|backup|copy|download|crdownload|part)$", re.I)
# '.key' is both a cryptographic key and an Apple Keynote deck: Keynote is a ZIP, a key file is not (see is_secret)
# values that look like keys are masked inside indexed text (a key pasted into a note or log never reaches the index)
# the name of a setting that holds a secret, with words before it (AWS_SECRET_ACCESS_KEY, client_secret, DB_PASSWORD) and
# at most two after it (DB_PASSWORD_PROD); 'tokenizer' or 'max_tokens' is not one
_KV_NAME = (r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.\-]){0,4}(?i:api[_-]?key|secret(?:[_-]?access)?(?:[_-]?key)?|password|passwd|passphrase|pwd"
            r"|token|private[_-]?key)(?:[_.\-][A-Za-z0-9]+){0,2}(?![A-Za-z0-9])")
# PEM / PGP private key: to its END line; with none (a note cut short), to the end of the text or to the first blank line
# that is not followed by more key lines (a PGP block has one blank line after its headers). Any case, any spacing, and its
# header split over two lines ('-----BEGIN RSA PRIVATE\nKEY-----')
_PEM_LINE = r"-----[ \t]*{0}\s+(?:[A-Z0-9]+\s+){{0,4}}?PRIVATE\s+KEY(?:\s+BLOCK)?[ \t]*-----"
_PEM = (r"(?i:" + _PEM_LINE.format("BEGIN") + r")(?:[\s\S]*?(?i:" + _PEM_LINE.format("END") + r")"
        r"|[\s\S]*?(?=\n[ \t]*\r?\n(?![ \t]*[A-Za-z0-9+/=]{20,}[ \t]*(?:\r?\n|$))|\Z))")
# a PEM block written out in base64 ('LS0tLS1CRUdJTi' is '-----BEGIN' in base64): the whole blob, over its lines
_PEM_B64 = r"LS0tLS1CRUdJTi[A-Za-z0-9+/=]*(?:\r?\n[A-Za-z0-9+/=]{8,})*"
# a value under a label that names a secret: the label is kept, the value masked whatever its length (red-team 0.2.24:
# 'password: correct horse battery staple', '비밀번호: ...', a short JSON "password": "..."). Quoted: what is inside the
# quotes; not quoted: to the end of the line (a phrase with spaces). Korean labels as well
_LABEL = r"(?:" + _KV_NAME + r"|(?:비밀번호|패스워드|비번|암호))"
_SEP = r"""['"]?\s*[:=：](?!=)"""
_LABELED = (r'(?P<lq>' + _LABEL + _SEP + r'[ \t]*")(?:\\.|[^"\\\r\n])+(?=")'
            r"|(?P<ls>" + _LABEL + _SEP + r"[ \t]*')(?:\\.|[^'\\\r\n])+(?=')"
            r"|(?P<lu>" + _LABEL + _SEP + r"""[ \t]*['"]?)(?=[^\s'"])[^\r\n]+"""
            # XML: <password>...</password>, and a KeePass entry's <Key>Password</Key><Value>...</Value>
            r"|(?P<lx><(?i:password|passwd|pwd|passphrase|secret|token|api[_-]?key|client[_-]?secret|private[_-]?key|비밀번호|암호)"
            r"(?:\s[^<>]*)?>)[^<]+(?=</)"
            r"|(?P<lk>(?i:<Key>\s*(?:password|passwd|비밀번호|암호)\s*</Key>\s*<Value(?:\s[^<>]*)?>))[^<]+(?=</Value>)")
KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}"
                     r"|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_\-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"
                     r"|xox[abposr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}|" + _PEM + r"|" + _PEM_B64 +
                     r"|Private-Lines:\s*\d+(?:\s+[A-Za-z0-9+/=]{4,}(?![\-:A-Za-z0-9+/=]))+"   # a PuTTY key's private part
                     r"|(?i:bearer)\s+[A-Za-z0-9\-._~+/]{16,}=*"
                     r"|eyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*"   # a JWT
                     r"|npm_[A-Za-z0-9]{30,}|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}|GOCSPX-[A-Za-z0-9_\-]{20,}"   # npm, SendGrid,
                     r"|ya29\.[A-Za-z0-9_\-]{20,}"   # a Google OAuth client secret and access token
                     r"|(?P<hook>hooks\.slack\.com/services/)[A-Za-z0-9/_\-]+"   # a Slack webhook
                     # Azure storage: AccountKey=..., SharedAccessSignature=..., a SAS URL's sig=...
                     r"|(?P<az>(?i:AccountKey|SharedAccessKey|SharedAccessSignature)\s*=\s*)[^;\s'\"<>]+"
                     r"|(?P<sig>(?<![A-Za-z0-9])sig=)[^&;\s'\"<>]+"
                     # Twilio: an SK / AC id or a 32-hex auth token after 'twilio' or 'auth' on the same line
                     r"|(?P<tw>(?i:\b(?:twilio|auth(?:[_ -]?token)?)\b)[^\r\n]{0,80}?)(?<![A-Za-z0-9])(?:SK|AC)?[0-9a-fA-F]{32}(?![0-9A-Za-z])"
                     # scheme://user:PASSWORD@host, the password up to the last '@' before the host (it may hold '@' and
                     # '/'); 'host:8080/path@x' is a port, not a password
                     r"""|(?P<url>\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@'"<>]+:)(?!\d+(?:[/?#\s'"<>]|$))[^\s'"<>]+(?=@[^\s@/'"<>]+(?:[:/?#\s'"<>]|$))"""
                     r"|" + _LABELED + r")")
_KEEP = ("url", "lq", "ls", "lu", "lx", "lk", "hook", "az", "sig", "tw")
# a fixed piece of text every KEYLIKE match contains: SQLite LIKE (ASCII case-insensitive, '_' any one character) keeps only the
# chunks that may hold one, so the regex runs on a few rows instead of all of them (a run with no change scanned 920k chunks
# in Python for 58 s, #67). Keep in step with KEYLIKE: tests/test_mask_prefilter.py checks every alternative is covered
KEYLIKE_HINTS = ("sk-", "akia", "gh_", "hf_", "xox", "aiza", "private", "api", "secret", "password", "passwd", "token",
                 "github_pat", "glpat", "k_live", "k_test", "private-lines", "bearer", "eyj", "://", "pwd", "private_key", "privatekey",
                 "-----", "ls0tls1crudjti", "npm_", "sg.", "gocspx-", "ya29.", "hooks.slack.com", "accountkey", "sharedaccess",
                 "sig=", "twilio", "auth", "비밀번호", "패스워드", "비번", "암호", "passphrase")


def _mask_one(m):
    return next((m.group(k) for k in _KEEP if m.group(k)), "") + "[가림]"


def mask(text):
    """values that look like keys, passwords and tokens replaced by [가림]: what the index stores and every text findr
    returns (read_file, excerpts, the search page). A URL keeps its address, only the password in it is masked. A mail's
    attachment block ('[첨부 name]' and its text) that the secret rules mark keeps its name only (text an index made
    before a rule may still hold)"""
    if not text:
        return text
    text = KEYLIKE.sub(_mask_one, text)
    return _ATT_BLOCK.sub(_att_one, text) if "[첨부 " in text else text


_ATT_BLOCK = re.compile(r"\[첨부 ([^\]\n]{1,300})\]\n(.*?)(?=\n\[첨부 [^\]\n]{1,300}\]\n|\Z)", re.S)


def _att_one(m):
    try:
        bad = is_secret(Path(m.group(1)), head=m.group(2)[:4096].encode("utf-8", "replace"))
        if not bad:   # a mail attached as a text file: a nested mail, its name only
            try:
                from .moretext import looks_like_mail
            except ImportError:
                from moretext import looks_like_mail
            bad = looks_like_mail(m.group(2))
    except Exception:   # noqa: BLE001
        bad = True
    return f"[첨부 {m.group(1)}]\n" if bad else m.group(0)


def mask_obj(o, keep=frozenset({"path", "name", "date", "modified", "folder", "fid", "route", "kind", "ext", "file_id", "family_id"})):
    """mask() on every text in a result (dicts and lists, as JSON holds them), except paths, names and other fields that
    say which file it is (a file is opened by its path)"""
    if isinstance(o, str):
        return mask(o)
    if isinstance(o, list):
        return [mask_obj(x, keep) for x in o]
    if isinstance(o, dict):
        return {k: (v if k in keep else mask_obj(v, keep)) for k, v in o.items()}
    return o
LIB = re.compile(r"/(site-packages|python_embeded|node_modules|__pycache__|\.git|dist-packages|\.venv|venv)(/|$)")
# folders of keys and sign-ins, never read, whatever folder was chosen (`findr index ~` read .kube/config, .docker/config.json
# and shell histories, whose tokens the key masking does not all catch, and served them to AI tools)
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".1password", "Keychains"}
SKIP_DIR = {".git", "__pycache__", "node_modules"} | SECRET_DIRS
# 2026-11-05 (user's decision): not the user's files -> not indexed by default (--with-libs keeps the old behaviour)
LIB_DIRS = {"site-packages", "python_embeded", "node_modules", "dist-packages", ".venv", "venv", "__pycache__", ".git",
            "dist", ".cache", ".next", ".pytest_cache", ".mypy_cache", ".tox"}
APP_MARK = {"python_embeded", "python_embedded"}
JUNK = re.compile(r"^(vocab\.json|merges\.txt|tokenizer(_config)?\.json|special_tokens_map\.json|vocab\.txt)$|(\.map|\.pyc|\.pyo|\.min\.js|\.min\.css)$|-[A-Za-z0-9_]{8}\.(js|css)$|^~\$|^(Thumbs\.db|\.DS_Store|desktop\.ini)$", re.I)
# backups keep the user's history ("bos 넣기 전 학습 스크립트"): read them as the kind of file they back up
BAK = re.compile(r"(\.(bak\w*|old|orig|backup|swp|tmp)|~)$", re.I)
MAXREAD = 2_000_000   # bytes of text read per tier-1 file
GIST = int(os.environ.get("FINDR_GIST") or 12_000)
RICH = {".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".key", ".doc", ".hwp", ".ppt", ".xls"}   # read through a parser: output length tells


def _size(s):
    try:
        return os.path.getsize(s)
    except OSError:
        return 0   # first pass: the opening of each document (its first pieces), the rest later
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")   # Apple GPU: operations it lacks run on the CPU instead of failing
FILE_SECONDS = float(os.environ.get("FINDR_FILE_SECONDS") or 60)   # time budget per file; what was read by then is kept
DATA = {".json", ".jsonl", ".csv", ".tsv", ".log"}   # machine-written results: the head says what they are
DATA_READ = 30_000
CHUNK, OVER = 900, 150


def real_ext(p: Path):
    """the extension that says how to read the file: train.py.bak_pack2 -> .py, 견적서.xlsx.old -> .xlsx"""
    name = p.name
    while BAK.search(name):
        name = BAK.sub("", name)
    return Path(name).suffix.lower() if name != p.name else p.suffix.lower()


def secret_path(s):
    """a path inside a certificate folder (NPKI, GPKI, EPKI, any case), or a certificate-like file (CERT_EXTS) inside a
    folder whose name holds 공동인증서 / 공인인증서. Both separators: a Windows path is checked the same on any PC (and the
    index may hold one from another)"""
    parts = re.split(r"[\\/]+", nfc(str(s)))
    if any(x.casefold() in CERT_DIRS for x in parts[:-1]):
        return True
    return real_ext(Path(parts[-1])) in CERT_EXTS and any(w in x for x in parts[:-1] for w in CERT_WORDS)


CRED_LINES = 5   # non-empty lines looked at for a credential header: an export may start with a title line


def cred_header(head):
    """True when one of the first CRED_LINES non-empty lines of a CSV / TSV / text file (bytes or text) is a credential
    export's header: two or more short columns, one a password column and one a user name or site column (English or
    Korean). Not only the first line: 'Saved sign-ins (exported ...)' above the header hid one (red-team 0.2.24)"""
    if isinstance(head, bytes):
        b = head[:4096]
        head = b.decode("utf-16", "replace") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else _decode_text(b)
    lines = [x for x in (head or "").replace("\ufeff", "").splitlines() if x.strip()][:CRED_LINES]
    return any(_cred_line(x) for x in lines)


def _cred_line(line):
    cols = [unicodedata.normalize("NFC", c).strip().strip('"\'').strip().casefold() for c in re.split(r"[,;\t|]", line)]
    cols = [re.sub(r"[\s_\-]+", "", c) for c in cols if c]
    if len(cols) < 2 or any(len(c) > 40 for c in cols):
        return False   # a sentence, not a header
    pw = [c for c in cols if c in _CRED_PW or any(w in c for w in _CRED_PW_IN)]
    who = [c for c in cols if c not in pw and (c in _CRED_ID or any(w in c for w in _CRED_ID_IN))]
    return bool(pw) and bool(who)


# password-manager exports by their content, whatever their name (red-team 0.2.24: vault.json, keepass_dump.xml): a KeePass
# XML, or JSON / XML records holding a password field beside a user name or site field (Bitwarden, 1Password .1pif, Firefox
# logins.json, any manager's JSON export)
PM_EXTS = {".json", ".jsonl", ".xml", ".txt", ".csv", ".tsv", ".1pif", ""}
_PM_KEEPASS = re.compile(r"<\s*KeePassFile\b", re.I)
_PM_PW = re.compile(r"(?i)\"(?:password|passwd|pwd|encryptedPassword|login_?password|passphrase)\"\s*:"
                    r"|\"designation\"\s*:\s*\"password\"|<\s*(?:password|passwd)\s*>")
_PM_WHO = re.compile(r"(?i)\"(?:username|user|login|email|uri|uris|url|hostname|origin|origin_?url|website|encryptedUsername"
                     r"|login_?username|login_?uri)\"\s*:|\"(?:designation|name)\"\s*:\s*\"(?:username|email|login)\""
                     r"|<\s*(?:username|user|login|email|url|uri|website)\s*>")


def pm_export(head):
    """True when text (or bytes) is a password manager's export by its content"""
    if isinstance(head, (bytes, bytearray)):
        b = bytes(head[:65536])
        head = b.decode("utf-16", "replace") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else _decode_text(b)
    t = (head or "")[:65536]
    return bool(_PM_KEEPASS.search(t)) or (bool(_PM_PW.search(t)) and bool(_PM_WHO.search(t)))


def secret_text(name, text):
    """the content rules on text already read (the content stage, and an index from before a rule): a credential export's
    header in a CSV / TSV / text file, or a password manager's export (pm_export) in a JSON / XML / text file"""
    forms = name_forms(base_name(name))
    exts = {real_ext(Path(f)) for f in forms}
    return bool((exts & CRED_EXTS and cred_header(text[:4096])) or (exts & PM_EXTS and pm_export(text)))


# characters a name can hold that show as nothing: '.env\u200b' read as '.env' to the eye and passed the name rules
# (red-team 0.2.24). Zero-width space / joiners, BOM, word joiner, soft hyphen and other format characters are dropped
_INVISIBLE = re.compile("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f"
                        "\u3164\ufe00-\ufe0f\ufeff\uffa0]")
# every Unicode space (no-break, ideographic U+3000, ...) as a plain one, so a trailing one is dropped like ' '
_SPACES = re.compile(r"\s")


def name_forms(name):
    """the forms of a file name the secret rules are tried on: NFC and case-folded, with trailing spaces and dots dropped
    (Windows ignores them: '.env ' is '.env'), backup endings, a second extension ('passwords.csv.txt') and copy markers
    ('Copy of logins.csv', 'logins 2.csv', '비밀번호 - 복사본.csv') taken off"""
    n = _INVISIBLE.sub("", nfc(str(name or ""))).casefold()
    n = _SPACES.sub(" ", n)
    n = n.rstrip(" .") or n
    forms, todo = [], [n]
    while todo and len(forms) < 24:
        x = todo.pop()
        if not x or x in forms:
            continue
        forms.append(x)
        todo.append(BAK.sub("", x).rstrip(" ."))
        m = _SECOND_EXT.match(x)
        if m:
            todo.append(m.group(1).rstrip(" ."))
        stem, ext = os.path.splitext(x)
        if ext and " " in ext:   # 'logins.csv 2' is not an extension
            stem, ext = x, ""
        s2 = _COPY_SUF.sub("", _COPY_PRE.sub("", stem)).rstrip(" .")
        if s2 and s2 != stem:
            todo.append(s2 + ext)
        s3 = _COPY_PRE.sub("", x)
        if s3 and s3 != x:
            todo.append(s3)
    return forms


_SECRET_DIRS_CF = {d.casefold() for d in SECRET_DIRS}


def base_name(name):
    """the last part of a name that may carry a folder in either OS's form: a mail attachment named
    'C:\\Users\\x\\secrets.json' or '../x/.env' on any PC (PurePosixPath takes the whole Windows path as the name)"""
    parts = [x for x in re.split(r"[\\/]+", str(name or "")) if x]
    return parts[-1] if parts else str(name or "")


def secret_name(p):
    """the rules that need only the path (names, certificate folders, folders of keys such as .ssh and .kube): what a
    secret file is, without opening it"""
    if any(SECRET.search(f) for f in name_forms(base_name(p.name))) or secret_path(p):
        return True
    parts = re.split(r"[\\/]+", nfc(str(p)))
    return any(x.casefold().rstrip(" .") in _SECRET_DIRS_CF for x in parts[:-1])


def _head(p):
    try:
        with open(p, "rb") as f:
            return f.read(4096)
    except OSError:
        return b""   # nothing to read: nothing to hide either


def is_secret(p: Path, head=None, sniff=True, follow=True):
    """key and credential files are never read; a '.key' file is a secret unless it is a Keynote deck (a ZIP); a CSV, TSV
    or text file whose first line is a credential export's header is secret whatever its name. `head`: the file's first
    bytes when they are at hand (a mail attachment); otherwise they are read from `p`. sniff=False reads the first line
    only of a file whose name speaks of passwords (the scan of names: the content stage looks at the others).
    follow: a link is judged by where it points as well (a harmless name linking to a key, or into .ssh)"""
    if secret_name(p):
        return True
    if follow and head is None:
        t = link_target(p)
        if t is not None and is_secret(Path(t), sniff=sniff, follow=False):
            return True
    forms = name_forms(base_name(p.name))
    exts = {real_ext(Path(f)) for f in forms}
    if exts & (CRED_EXTS | PM_EXTS) and (head is not None or sniff or any(CRED_NAME.search(f) for f in forms)):
        h = _head(p) if head is None else head
        if (exts & CRED_EXTS and cred_header(h)) or (exts & PM_EXTS and pm_export(h)):
            return True
    if real_ext(p) == ".key":
        try:
            with open(p, "rb") as f:
                return f.read(4) != b"PK\x03\x04"
        except OSError:
            return True
    return False


def link_target(p):
    """where a path really leads when it is, or goes through, a link (or a junction, a Windows short name); None when it
    leads where it says"""
    try:
        a = os.path.abspath(str(p))
        r = os.path.realpath(a)
    except (OSError, ValueError):
        return None
    return None if os.path.normcase(r) == os.path.normcase(a) else r


def tier(p: Path, sniff=False):
    s = p.as_posix(); ext = real_ext(p)
    if is_secret(p, sniff=sniff, follow=os.path.islink(p)):   # the walk does not go through linked folders: only the file itself can be a link
        return 0
    if ext in AUD:
        return 1 if transcript(p) is not None else 3
    if ext not in TEXT and ext not in DOC:
        return 3
    if LIB.search(s):
        return 2
    return 1


try:
    from . import relations, samepath
except ImportError:
    import relations
    import samepath

try:   # old office formats: content sniffing + Word 97 piece table before LibreOffice (orders of magnitude faster)
    from .officetext import sniff_text, doc_text, hwp_text
    from .moretext import sheet_text, rtf_text, eml_text, emlx_text, mbox_text
except ImportError:
    from officetext import sniff_text, doc_text, hwp_text
    from moretext import sheet_text, rtf_text, eml_text, emlx_text, mbox_text


def _long(s: str) -> str:
    """Windows: the \\\\?\\ form of an absolute path, which reaches past 260 characters without the LongPathsEnabled setting
    (off by default: files deeper than that were left out silently, #60). Elsewhere, and for relative paths: unchanged"""
    if os.name != "nt" or s.startswith("\\\\?\\") or not os.path.isabs(s):
        return s
    s = os.path.normpath(s)
    return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s


def _short(s: str) -> str:
    """back from the \\\\?\\ form: paths are stored, shown and opened in their usual form"""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    return s[4:] if s.startswith("\\\\?\\") else s


def extract(p: Path, limit):
    ext = real_ext(p)
    if os.name == "nt" and len(str(p)) >= 248:   # past MAX_PATH: readers open it in the \\?\ form
        p = Path(_long(str(p)))
    try:
        if ext in (".xls", ".xlsb", ".ods"):   # sheets: python-calamine first, LibreOffice only when that fails
            t = sheet_text(p, limit)
            if t and t.strip():
                return t
            if ext != ".xls":
                return ""
        if ext == ".rtf":
            return rtf_text(p, limit) or ""
        if ext == ".eml":
            return eml_text(p, limit) or ""
        if ext == ".emlx":
            return emlx_text(p, limit) or ""
        if ext == ".mbox" or (p.name == "mbox" and p.parent.suffix.lower() == ".mbox"):   # Apple Mail export: X.mbox/mbox
            return mbox_text(p, limit) or ""
        if ext in (".pages", ".numbers"):   # iWork shares Keynote's container: snappy-framed protobuf in Index/*.iwa
            return keynote_text(p, limit)
        if ext in OLD:
            t = sniff_text(p, limit)   # web-exported HTML/MHT/text saved under an office extension
            if t is None and ext == ".doc":
                t = doc_text(p, limit)   # real Word 97-2003: piece table, no LibreOffice
            if t is None and ext == ".hwp":
                t = hwp_text(p, limit)   # HWP 5: compressed body records, no LibreOffice
            return t if t and t.strip() else convert_old(p)[:limit]
        if ext in AUD:
            return (transcript(p) or "")[:limit]
        if ext == ".pdf":
            return pdf_text(p, limit)
        if ext == ".key":
            return keynote_text(p, limit)
        if ext in (".docx", ".xlsx", ".pptx", ".hwpx"):
            return ooxml_text(p, limit)
        raw = open(p, "rb").read(limit)
        if b"\x00" in raw[:4096] and raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):   # binary, unless UTF-16 text with its BOM
            return ""
        t = _decode_text(raw)   # Korean Notepad files are often 'ANSI' (cp949) or '유니코드' (UTF-16)
        if ext in (".html", ".htm"):
            t = re.sub(r"(?s)<(script|style).*?</\1>", " ", t); t = re.sub(r"<[^>]+>", " ", t)
        if ext == ".ipynb":
            try:
                t = "\n".join("".join(c.get("source", "")) for c in json.loads(t).get("cells", []))
            except Exception:
                pass
        return t
    except (PermissionError, BlockingIOError):
        raise   # open in another program right now: _read_one keeps it pending for the next run
    except OSError as e:
        if getattr(e, "winerror", None) in (32, 33):   # Windows sharing / lock violation (a document open in Office)
            raise
        print("  extract fail", p, type(e).__name__, file=sys.stderr)
        return ""
    except Exception as e:
        print("  extract fail", p, type(e).__name__, file=sys.stderr)
        return ""


def transcript(p: Path):
    """the cached local transcript of an audio file, or None when it has not been transcribed yet"""
    import hashlib
    try:
        st = p.stat()
    except OSError:
        return None
    c = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "conv" / (hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest() + ".txt")
    return c.read_text(encoding="utf-8", errors="ignore") if c.exists() else None


# What the readers can read changes between versions. An unchanged file (same size, mtime and tier) is never read again,
# so files these readers newly handle are queued once when the reader version goes up.
#   2: .xls/.xlsb/.ods without LibreOffice (#29), distribution(배포용) .hwp (#28)
#   3: mail attachments under the secret rules and nested mail by name only; password exports found by their first line
#   4: credential headers below a title line, password-manager exports by their content (secret_text)
READERS = 4
REREAD_EXT = {2: (".xls", ".xlsb", ".ods", ".hwp")}
REREAD_ALL = {3: (".eml", ".emlx", ".mbox")}   # read again even with text stored: what they stored may now be left out


def reread_for_new_readers(con):
    """files whose stored text is empty and whose kind a newer reader handles go back to 'pending' (read in the content wave)"""
    have = int((con.execute("SELECT value FROM meta WHERE key='readers'").fetchone() or ["1"])[0])
    if have >= READERS:
        return 0
    exts = sorted({e for v, es in REREAD_EXT.items() if v > have for e in es})
    n = con.execute(f"""UPDATE files SET pending=1 WHERE tier=1 AND ext IN ({','.join('?' * len(exts))})
                        AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.file=files.id AND length(c.text) > 0)""", exts).rowcount if exts else 0
    every = sorted({e for v, es in REREAD_ALL.items() if v > have for e in es})
    if every:
        n += con.execute(f"UPDATE files SET pending=1 WHERE tier=1 AND ext IN ({','.join('?' * len(every))})", every).rowcount
    if have < 4:   # files already read that the content rules now mark (a password export's header, by its first lines
        # since 4): read again, so the content stage keeps the name only
        hit = [fid for fid, name, t in con.execute("""SELECT f.id, f.name, c.text FROM files f JOIN chunks c ON c.file=f.id AND c.n=0
                                                      WHERE f.tier=1""")
               if t and secret_text(name, t)]
        for fid in hit:
            con.execute("UPDATE files SET pending=1 WHERE id=?", (fid,))
        n += len(hit)
    con.execute("INSERT OR REPLACE INTO meta VALUES('readers', ?)", (str(READERS),)); con.commit()
    if n:
        print(f"FINDR_REREAD {n} files a newer reader can read ({', '.join(exts)})", flush=True)
    return n


def _hangul_like(t: str) -> bool:
    """bytes read as cp949 really were Korean: most non-ASCII characters are Hangul syllables (Latin-1 bytes read as cp949
    come out as Hanja and symbols)"""
    na = [c for c in t if ord(c) > 127]
    return bool(na) and sum("가" <= c <= "힣" for c in na) >= 0.6 * len(na)


def _decode_text(b: bytes) -> str:
    """text of unknown encoding, as Notepad and converters write it: UTF-16 with a BOM ('유니코드'), UTF-8 (with or without
    BOM), else this PC's code page ('ANSI'), else Korean cp949 when it reads as Korean, else UTF-8 dropping bad bytes.
    b may be cut at a read limit: a character cut in half at the end does not count as a wrong guess"""
    import codecs
    import locale
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "ignore")
    if b[:3] == codecs.BOM_UTF8:
        b = b[3:]
    sys_enc = (locale.getpreferredencoding(False) or "utf-8").lower()
    cands = [("utf-8", 3, None)]
    if codecs.lookup(sys_enc).name != "utf-8":
        cands.append((sys_enc, 1, _hangul_like if codecs.lookup(sys_enc).name in ("cp949", "euc_kr") else None))
    if codecs.lookup("cp949").name not in [codecs.lookup(e).name for e, _, _ in cands]:
        cands.append(("cp949", 1, _hangul_like))
    for enc, cut, ok in cands:
        try:
            t = b.decode(enc)
        except UnicodeDecodeError as e:
            if e.start < len(b) - cut:
                continue
            t = b[:e.start].decode(enc, "ignore")
        if ok is None or ok(t):
            return t
    return b.decode("utf-8", "ignore")


LO_SLOTS = 16   # LibreOffice profiles kept in the data folder, one per reader process at a time
_LO = []        # this process's profile: [argument, open lock file]


def lo_profile():
    """a LibreOffice profile of this reader process's own (["-env:UserInstallation=..."]), or [] when none can be had.
    Reader processes converting at the same time shared the person's one profile: the second LibreOffice handed its file to
    the first or stopped on the profile lock. Slots are reused across runs (a lock file each, released when the process
    ends), so the data folder holds at most LO_SLOTS profiles"""
    if _LO:
        return [_LO[0]]
    d = os.environ.get("FINDR_DATA")
    if not d:
        return []
    root = Path(d, "lo")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    for k in range(LO_SLOTS):
        f = open(root / f"slot{k}.lock", "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            continue
        _LO[:] = ["-env:UserInstallation=" + (root / f"slot{k}").resolve().as_uri(), f]
        return [_LO[0]]
    return []


def convert_old(p: Path):
    """hwp/doc/ppt/xls -> text with a local LibreOffice; cached by path, size and mtime (re-runs cost nothing)"""
    import hashlib
    import shutil
    import tempfile
    st = p.stat()
    key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    cache = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "conv" / (key + ".txt")
    done = cache.with_suffix(".ok")   # LibreOffice ran and the file really holds no text
    fail = cache.with_suffix(".fail")   # LibreOffice ran and could not convert it: not tried again with the same LibreOffice
    so = shutil.which("soffice")
    try:
        so_id = f"{so}|{Path(so).resolve().stat().st_mtime}" if so else ""
    except OSError:
        so_id = so or ""
    if cache.exists() and (cache.stat().st_size or done.exists() or not so
                           or (fail.exists() and fail.read_text(errors="ignore") == so_id)):   # a failed (empty) result is retried
        return cache.read_text(encoding="utf-8", errors="ignore")
    if not so:
        return ""   # nothing to convert with: no cache entry, so installing LibreOffice later reads the file
    cache.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower(); txt = ""; converted = False
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("in" + ext); shutil.copyfile(p, src)
        # plain 'txt:Text' is written in the system code page (cp949 on Korean Windows): read as UTF-8, every Korean
        # character was dropped. Ask for UTF-8, and still read an older LibreOffice's output in the local code page
        target = "pdf" if ext in (".ppt", ".xls") else "txt:Text (encoded):UTF8"
        try:
            subprocess.run(["soffice", *lo_profile(), "--headless", "--convert-to", target, "--outdir", td, str(src)], capture_output=True, timeout=120)
            converted = (Path(td) / ("in.pdf" if target == "pdf" else "in.txt")).exists()
            if target == "pdf" and converted:
                txt = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", str(Path(td) / "in.pdf"), "-"],
                                     capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
            elif converted:
                txt = _decode_text((Path(td) / "in.txt").read_bytes())
        except Exception as e:
            print("  convert fail", p.name, type(e).__name__, file=sys.stderr)
    cache.write_text(txt, encoding="utf-8")
    if converted and not txt.strip():
        done.touch()   # really empty: not converted again on every run
    elif not converted:
        fail.write_text(so_id, encoding="utf-8")   # up to 120 s each time otherwise; a new LibreOffice gets another try
    return txt


def chunks(text):
    text = text.encode("utf-8", "replace").decode("utf-8")   # a lone surrogate from any reader would stop the whole index
    text = mask(text)
    text = re.sub(r"[ \t]{3,}", "  ", text).strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK]); i += CHUNK - OVER
    return out


def nfc(s):
    """one form for Korean (macOS writes names decomposed, people type them composed): what search compares is NFC"""
    return unicodedata.normalize("NFC", s) if s else s


def label(p: Path):   # what the encoder sees besides the text: names carry meaning ("LoRA_실험_요약.pdf")
    return nfc(f"{p.name} | {'/'.join(p.parts[-4:-1])}")


WAL_LIMIT = 64 * 2 ** 20   # bytes the write-ahead file keeps after a checkpoint
_RUN = {"con": None}


def wal_trim(con):
    """after a large write: move the write-ahead file into the index and cut it to nothing (best effort: a reader in the
    middle of a question keeps its part until the next time)"""
    try:
        con.commit()
        con.execute("PRAGMA busy_timeout=2000")   # not the minute a write waits: a question reading now is not waited out
        try:
            return con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.execute("PRAGMA busy_timeout=60000")
    except sqlite3.Error:
        return None


def switch_room_gb(con):
    """disk space a rewrite of every row takes in the write-ahead file: about the index itself (0 when it cannot tell)"""
    try:
        pages = con.execute("PRAGMA page_count").fetchone()[0]
        size = con.execute("PRAGMA page_size").fetchone()[0]
        return pages * size / 2 ** 30
    except sqlite3.Error:
        return 0.0


def main():
    """_main, stopping cleanly where the run gives the PC back or the disk runs out: what was stored stays (committed),
    exit 7 (person back, on battery) or 8 (disk nearly full)"""
    try:
        return _main()
    except GiveBack as e:
        con = _RUN["con"]
        try:
            if con is not None:
                con.commit()
        except sqlite3.Error:
            con.rollback()
        if e.why == "disk":
            print(f"FINDR_DISKFULL 색인 폴더의 디스크 여유가 {e.detail}뿐이라 색인을 멈췄습니다. 지금까지 읽은 것은 남아 있고, "
                  f"공간이 생기면 이어서 합니다. (disk nearly full: indexing stopped, what was read is kept)", flush=True)
        else:
            print(f"FINDR_YIELD {e.why}: the run stops here and keeps what it stored; the app runs it again later", flush=True)
        if con is not None:
            wal_trim(con)
        return GIVE_BACK_CODE.get(e.why, 7)
    except sqlite3.OperationalError as e:
        if "full" not in str(e).lower():
            raise
        con = _RUN["con"]   # the disk filled between two looks: the last batch is undone, the rest stays
        try:
            if con is not None:
                con.rollback()
        except sqlite3.Error:
            pass
        print(f"FINDR_DISKFULL 디스크가 가득 차서 색인을 멈췄습니다. 지금까지 저장한 것은 남아 있습니다. (database or disk is full)", flush=True)
        return 8


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, nargs="+", help="one or more folders, in the order people look in them")
    ap.add_argument("--db", required=True)
    ap.add_argument("--model", default="embeddinggemma-2"); ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--with-libs", action="store_true", help="also index bundled libraries and build output (head only)")
    # 2026-11-05: newest and most useful first. The scan registers every file by name at once; content is read in waves.
    ap.add_argument("--max-age-days", type=float, default=0, help="this wave reads content only of files changed within N days (0 = all)")
    ap.add_argument("--limit", type=int, default=0, help="read content of at most N pending files in this run (0 = no limit)")
    ap.add_argument("--upgrade", default="", help="move the meaning vectors to this encoder in idle time (no scan), then switch")
    ap.add_argument("--minutes", type=float, default=50, help="--upgrade: stop after N minutes (the next idle run continues)")
    ap.add_argument("--upgrade-first", action="store_true", help="--upgrade: only each file's first piece, and no switch (exit 6 when all are there)")
    ap.add_argument("--names-only", action="store_true", help="register new and changed files by name only (on battery); content waits for the next full run")
    ap.add_argument("--gone-only", action="store_true", help="only forget files deleted since the last check: the indexed folders whose time changed are listed, nothing is read")
    a = ap.parse_args()
    roots = [Path(r) for r in a.root]; Path(a.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    if not one_indexer(a.db):
        print("FINDR_BUSY 다른 색인 작업이 이 색인을 쓰고 있습니다. 끝난 뒤에 다시 실행해 주세요.", flush=True)
        sys.exit(3)
    miss = missing_parts()
    if miss:   # once per run, so a quiet gap has a reason and a fix
        print("FINDR_WARN missing: " + ", ".join(miss) + " -> findr doctor (설치 명령을 알려 줍니다)", flush=True)
    con = sqlite3.connect(Path(a.db).expanduser(), timeout=60)
    _RUN["con"] = con
    _GB.update(t0=time.time(), looked=0.0, disk=0.0, db=str(Path(a.db).expanduser().parent))   # this run's clock
    con.execute("PRAGMA journal_mode=WAL")   # the search server keeps reading while the index is updated
    # the write-ahead file is cut back to this size after a checkpoint: after a large write (a whole deep run, the switch
    # to another encoder) it otherwise kept its largest size on the disk for as long as the engine had the index open
    con.execute(f"PRAGMA journal_size_limit={WAL_LIMIT}")
    con.executescript("""
      CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
      CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
      CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
      CREATE INDEX IF NOT EXISTS ch_file ON chunks(file);
      CREATE INDEX IF NOT EXISTS ch_n ON chunks(n, file);
      CREATE INDEX IF NOT EXISTS ch_noemb ON chunks(n, file) WHERE emb IS NULL;   -- what embed() looks for: no full pass over vectors
      CREATE TABLE IF NOT EXISTS cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0);""")
    if "pending" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN pending INT DEFAULT 0")
    if "emb2" not in [r[1] for r in con.execute("PRAGMA table_info(chunks)")]:   # the larger model's vectors, filled in idle time
        con.execute("ALTER TABLE chunks ADD COLUMN emb2 BLOB")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if not con.execute("SELECT 1 FROM meta WHERE key='nfc'").fetchone():   # an index made before NFC: moved once (#12)
        start = int((con.execute("SELECT value FROM meta WHERE key='nfc_upto'").fetchone() or ["0"])[0])   # an interrupted move goes on from here
        rows = con.execute("SELECT id, name, path FROM files WHERE id > ? ORDER BY id", (start,)).fetchall()
        moved, t0 = 0, time.time()
        for i, (fid, name, path) in enumerate(rows, 1):
            if nfc(name) != name or nfc(path) != path:
                con.execute("UPDATE files SET name=? WHERE id=?", (nfc(name), fid))
                for cid, text in con.execute("SELECT id, text FROM chunks WHERE file=?", (fid,)).fetchall():
                    con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
                    con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(name), nfc(path), nfc(text or "")))
                con.execute("UPDATE chunks SET emb=NULL WHERE file=? AND n < 8", (fid,))   # the name is part of the meaning vector
                moved += 1
            if i % 2000 == 0 or i == len(rows):   # minutes on a large Mac index: show it moving, and keep what is done if it is stopped
                con.execute("INSERT OR REPLACE INTO meta VALUES('nfc_upto', ?)", (str(fid),)); con.commit()
                progress("nfc", i, len(rows))
                print(f"FINDR_NFC_PROGRESS {i}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
        con.execute("INSERT OR REPLACE INTO meta VALUES('nfc', '1')"); con.execute("DELETE FROM meta WHERE key='nfc_upto'"); con.commit()
        if moved:
            print(f"FINDR_NFC {moved} files with decomposed (NFD) names moved to NFC", flush=True)
    reread_for_new_readers(con)
    if a.model in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (a.model,))   # known from the start: search works while indexing (#9)
    key = (lambda x: nfc(x).casefold()) if os.name == "nt" else nfc   # Windows folder names ignore case
    excl = {key(Path(x).as_posix()).rstrip("/") for x in os.environ.get("FINDR_EXCLUDE", "").split(os.pathsep) if x}
    try:   # findr's own checkout and data folder inside a chosen folder: not the person's files (#66); a chosen root itself stays
        from scope import own_dirs
    except ImportError:
        from .scope import own_dirs
    try:
        from scope import is_findr_checkout
    except ImportError:
        from .scope import is_findr_checkout
    # other copies of findr already in the index (a second clone, an older install, a worktree: #119), found by their
    # pyproject.toml rows; like the running checkout, they leave unless a chosen root is one of them or lies inside one
    copies = {Path(raw).parent for (raw,) in con.execute("SELECT path FROM files WHERE name='pyproject.toml'") if is_findr_checkout(Path(raw).parent)}
    owns = list(dict.fromkeys([*own_dirs(), *copies]))
    samepath.clear()
    own_ids = samepath.ids_of(owns)

    def as_own(r):
        """a chosen folder spelled as the own folder it is in or is (by identity: a firmlink, another letter case, a link)"""
        d = samepath.under(r, own_ids, follow=False)
        if d is None:
            return None
        for a in samepath.ancestors(r):
            if samepath.ident(a) == samepath.ident(str(d)):
                rel = os.path.relpath(os.path.abspath(str(r)), a)
                return key((Path(d) if rel == "." else Path(d) / rel).as_posix()).rstrip("/")
        return None
    rkeys = [(key(Path(r).resolve().as_posix()).rstrip("/"), key(Path(r).as_posix()).rstrip("/")) for r in roots]
    rkeys += [(o, key(Path(r).as_posix()).rstrip("/")) for r in roots if (o := as_own(r)) and o not in {x for x, _ in rkeys}]
    # a chosen folder inside one of them (an app that embeds findr keeps the person's files in its data folder) no longer lets
    # the whole data folder in: the rest of it (index, models, logs, settings) stays out, folder by folder (#200)
    held = {}   # own folder -> the chosen folders inside it, as resolved and as given
    for d in owns:
        k = key(d.as_posix()).rstrip("/")
        inside = [(rs, rg) for rs, rg in rkeys if rs == k or rs.startswith(k + "/")]
        if not inside:
            excl.add(k)
            # the same folder as a chosen folder above it spells it (/System/Volumes/Data/..., another case): its rows
            # already in the index under that spelling leave too, and the walk leaves it out by text as well as identity
            for r in roots:
                v = samepath.variant(d, r)
                if v:
                    excl.add(key(Path(v).as_posix()).rstrip("/"))
        elif all(rs != k for rs, _ in inside):   # a chosen root that is the folder itself keeps all of it
            held[k] = [x for pair in inside for x in pair]
    # the same folders by identity, for spellings no text compare can see (hard links, bind mounts, case, firmlinks)
    excl_ids = {i: d for i, d in own_ids.items() if key(Path(d).as_posix()).rstrip("/") in excl}
    held_ids = {i: d for i, d in own_ids.items() if key(Path(d).as_posix()).rstrip("/") in held}
    root_ids = samepath.ids_of(roots)

    def own_by_identity(full, sp):
        """a folder met in the walk that is one of findr's own under another spelling: an excluded one is left out; a held
        one only when not spelled as held (the chosen folders inside it are walked as their own roots)"""
        i = samepath.ident(full)
        if i is None:
            return False
        if i in excl_ids:
            return True
        return i in held_ids and sp != key(Path(held_ids[i]).as_posix()).rstrip("/")

    def left_out(s):
        """an excluded folder or anything under it; inside a held folder, all but the chosen folders and the way down to them"""
        if s in excl or any(s.startswith(x + "/") for x in excl):
            return True
        return any((s == k or s.startswith(k + "/")) and not any(s == r or s.startswith(r + "/") or r.startswith(s + "/") for r in keep)
                   for k, keep in held.items())
    if excl or held:   # folders this PC chose not to search (exclude.txt): files already indexed under them leave the index
        ids = [i for i, raw in con.execute("SELECT id, path FROM files") if left_out(key(raw).rstrip("/"))]
        for fid in ids:
            drop(con, fid)
        if ids:
            print(f"FINDR_EXCLUDE removed {len(ids)} files", flush=True)
    if os.environ.get("FINDR_SCOPE") == "user" and not a.upgrade:
        pres = [key(r.as_posix()).rstrip("/") + "/" for r in roots]
        rows = con.execute("SELECT id, path FROM files").fetchall()
        # a file whose folder is not there right now (a drive or share offline) is kept: only folders no longer chosen leave
        out = [i for i, raw in rows if not any(key(raw).startswith(x) for x in pres) and os.path.isdir(_long(str(Path(raw).parent)))]
        if out and len(out) <= len(rows) // 2:   # more than half would be a missing drive or a bad root list: kept
            for fid in out:
                drop(con, fid)
            print(f"FINDR_OUTSIDE removed {len(out)} files under folders no longer searched", flush=True)
        elif out:
            print(f"FINDR_OUTSIDE kept {len(out)} files outside the current folders (too many to be a scope change)", flush=True)
    con.commit()
    if a.gone_only:
        gone_check(con, roots)
        return 0
    if a.upgrade:
        return upgrade(con, a.upgrade, a.minutes, first_only=a.upgrade_first)
    progress("names", 0, len(roots))
    scanned = 0   # files looked at in the folders before this one
    fresh, lost = [], []   # files new to the index and files no longer found, over every root: a pair of them is a move
    for k, root in enumerate(roots):   # stage 1: names and folders of every chosen folder first (seconds)
        print("FINDR_ROOT " + json.dumps({"i": k, "n": len(roots), "path": str(root)}, ensure_ascii=False), flush=True)
        if not os.path.isdir(_long(str(root))):   # unplugged disk, network drive offline: its files stay as they were
            print(f"FINDR_ROOT_MISSING {root} (kept as indexed)", flush=True)
            progress("names", k + 1, len(roots)); continue
        seen, t0, new = set(), time.time(), 0
        pre = root.as_posix().rstrip("/") + "/"   # several roots share one db: only this root's files can be "gone"
        known = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute("SELECT path,size,mtime,id,tier FROM files WHERE path LIKE ? ESCAPE '\\'", (like_pre(pre),))}
        unread = []   # folders the walk could not open: counted, never silent
        for dpl, dns, fns in os.walk(_long(str(root)), onerror=unread.append):
            dp = _short(dpl)
            if not a.with_libs and APP_MARK & set(dns):
                dns[:] = []; continue   # an installed application bundle (ships its own Python): not the user's files
            dns[:] = [d for d in dns if d not in (SKIP_DIR if a.with_libs else SKIP_DIR | LIB_DIRS)]
            if os.name == "nt":   # os.walk skips symlinks but follows junctions ('Documents/My Music' -> Music, a second scan)
                from scope import is_link
                dns[:] = [d for d in dns if not is_link(os.path.join(dpl, d))]
            if os.environ.get("FINDR_SCOPE") == "user":   # whole-PC mode: no system, program, cache or library folders
                from scope import skip_dir, app_profile
                if app_profile(fns):
                    dns[:] = []; continue   # an app's embedded-browser profile (CapCut's CEF folder in ~/Movies)
                # the parent's path from the root: system names (windows, users, drivers, samsung ...) are skipped only at the
                # top. rel was "" below the top, which skip_dir took as the top too: a person's own 'users' or 'drivers'
                # folder anywhere in a project left the index (Mac Air, 11/9: 300 files of a web app's users/ code)
                try:
                    rel = "/" if Path(dp) == root else (Path(dp).relative_to(root).as_posix() or "?")
                except ValueError:
                    rel = "?"   # not under the root as written (a different spelling): below the top all the same
                dns[:] = [d for d in dns if not skip_dir(rel, d)]
            if excl or held:
                dns[:] = [d for d in dns if not left_out(key((Path(dp) / d).as_posix()))]
            if excl_ids or held_ids:
                dns[:] = [d for d in dns if not own_by_identity(os.path.join(dpl, d), key((Path(dp) / d).as_posix()))]
            # a copy of findr met during the walk (#119): only folders holding a pyproject.toml are looked at, which is rare
            dns[:] = [d for d in dns if not (os.path.isfile(os.path.join(dpl, d, "pyproject.toml")) and is_findr_checkout(Path(dp) / d))]
            for fn in fns:
                if not a.with_libs and JUNK.search(fn):
                    continue
                p = Path(dp) / fn
                if held and left_out(key(p.as_posix())):
                    continue   # the data folder's own files on the way down to a chosen folder inside it
                try:
                    st = os.stat(os.path.join(dpl, fn))
                except OSError:
                    continue
                if not _stat.S_ISREG(st.st_mode):
                    continue   # sockets, pipes, devices: nothing to read (CEF's SingletonSocket failed here)
                s = p.as_posix()
                try:
                    s.encode("utf-8")
                except UnicodeEncodeError:   # a name that is not valid text (Linux bytes): skipped, it crashed every run
                    continue
                seen.add(s)
                tick("names", k, len(roots), scanned=scanned + len(seen), folder=_short(dpl)[len(str(root)):].lstrip("/\\")[:80] or root.name)
                if os.path.islink(os.path.join(dpl, fn)):   # a link: where it leads decides too
                    t_ = samepath.unfirm(os.path.realpath(os.path.join(dpl, fn)))
                    if samepath.under(t_, excl_ids, follow=False) or left_out(key(Path(t_).as_posix())):
                        continue   # into findr's own folders: not the person's file
                tr = tier(p)
                if tr in (1, 2) and os.path.islink(os.path.join(dpl, fn)) and samepath.under(t_, root_ids, follow=False) is None:
                    tr = 3   # leads outside the chosen folders: found by its name, never read
                if tr == 1 and s in known and known[s][3] == 0:
                    tr = tier(p, sniff=True)   # found secret by its first line when read: still so unless that changed
                if tr in (1, 2) and cloud_only(st):
                    tr = 3   # OneDrive 'online-only': reading it would download it; found by name until it is on the PC
                if s in known and known[s][0] == st.st_size and abs(known[s][1] - st.st_mtime) < 1 and known[s][3] == tr:
                    continue   # unchanged (a changed tier rule re-reads the file; compared with the stored tier, so an
                    # online-only file is no longer dropped and added again on every run)
                if s in known:   # changed: drop old rows
                    fid = known[s][2]
                    drop(con, fid)
                cur = con.execute("INSERT INTO files(path,name,ext,size,mtime,tier,pending) VALUES(?,?,?,?,?,?,?)",
                                  (s, nfc(p.name), real_ext(p), st.st_size, st.st_mtime, tr, 1 if tr in (1, 2) else 0))
                fid = cur.lastrowid   # name and folder searchable at once; content comes in its wave
                cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, 0, "")).lastrowid
                if tr != 0:   # a secret file is not in the word index at all (fts_no_secrets)
                    con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p.name), nfc(s[len(root.as_posix()):]), ""))
                if s not in known:
                    fresh.append((fid, s, st.st_size, st.st_mtime, real_ext(p), tr, root))
                new += 1
                if new % 500 == 0:
                    pause_wait(give_back=False)
                if new % 5000 == 0:
                    con.commit(); print(f"  {new} files {time.time() - t0:.0f}s", flush=True)
        # folders the walk could not open keep their files (a share that dropped mid-walk is not a deletion)
        cut = [Path(_short(getattr(e, "filename", "") or "")).as_posix().rstrip("/") + "/" for e in unread if getattr(e, "filename", None)]
        gone = [k for k in known if k not in seen and not any(k.startswith(c) for c in cut)]
        lost += [(s, known[s]) for s in gone]   # dropped after every root is walked, unless the file turns up elsewhere
        con.commit()
        print(f"FINDR_SCAN new/changed {new} removed {len(gone)} in {time.time() - t0:.0f}s")
        if unread:
            print(f"FINDR_SKIPPED {len(unread)} folders could not be read ({type(unread[0]).__name__})", flush=True)
        scanned += len(seen)
        progress("names", k + 1, len(roots), scanned=scanned)
    reused = reuse_moved(con, fresh, lost)
    for s, kn in lost:
        if kn[2] not in reused:
            drop(con, kn[2])
    fts_no_secrets(con)
    con.commit()
    if reused:
        print(f"FINDR_MOVED {len(reused)} files moved or renamed: their text was kept, only their vectors are made again", flush=True)
    _P["files"] = con.execute("SELECT count(*) FROM files WHERE tier>0").fetchone()[0]
    if os.environ.get("FINDR_ORIGIN", "1") != "0":   # where files came from (mail, messenger, a site), as the PC recorded it
        try:
            import origin
            t1 = time.time(); got = origin.collect(con)
            print(f"FINDR_ORIGIN {json.dumps(got)} {time.time() - t1:.1f}s", flush=True)
        except Exception as e:   # a record the PC keeps is a bonus: never a reason for the index run to fail
            print(f"FINDR_ORIGIN skipped ({type(e).__name__})", flush=True)
    if not a.no_embed:   # stage 1b: a meaning vector from each file's name and folders (natural-language search on names)
        print("FINDR_STAGE namevec", flush=True); embed(con, a.model, names_only=True)
    if a.names_only:   # 2026-11-08 (#106): on battery the app still shows new files by name; their content stays pending
        progress("done", 1, 1); print("FINDR_STAGE done (names only)", flush=True); return
    print("FINDR_STAGE content", flush=True)
    for k, root in enumerate(roots):   # stage 2: content, the folders people look in first
        progress("content", k, len(roots)); read_wave(con, root, a)
    remask(con)
    fts_no_secrets(con)
    if not a.no_embed:   # stage 3: meaning (each file's opening pieces first)
        print("FINDR_STAGE meaning", flush=True); embed(con, a.model)
    if os.environ.get("FINDR_OCR", "1") != "0":   # stage 4: text in scans and screenshots
        print("FINDR_STAGE ocr", flush=True)
        for k, root in enumerate(roots):
            progress("ocr", k, len(roots)); ocr_wave(con, root)
        if not a.no_embed:
            embed(con, a.model)
    progress("done", 1, 1); print("FINDR_STAGE done", flush=True)
    if os.environ.get("PHOTO_URL"):   # photos: one meaning vector each from the local photo model (no request seen)
        embed_photos(con, os.environ["PHOTO_URL"])
    print("FINDR_STATS", dict(con.execute("SELECT tier,count(*) FROM files GROUP BY tier").fetchall()),
          "chunks", con.execute("SELECT count(*) FROM chunks").fetchone()[0])
    wal_trim(con)


STAGES = ("nfc", "names", "namevec", "content", "meaning", "ocr")
_P = {"stages": {}, "started": time.time()}   # this run's view of every stage, for the app and the search page


def progress(stage, done, total, **kw):
    """where the index stands, for the app and the search page; written to the data folder (atomically)
    stages: done / now / later (left for idle time) / (absent: waiting). files: how many files this PC has."""
    d = os.environ.get("FINDR_DATA")
    if not d:
        return
    st = _P["stages"]
    if stage == "done":
        for k in STAGES:
            if st.get(k) == "now":
                st[k] = "done"
    elif stage in STAGES:
        for k in STAGES[:STAGES.index(stage)]:   # every earlier stage is finished (or was left for later)
            if st.get(k) in (None, "now") and k != "nfc":
                st[k] = "done"
        if st.get(stage) != "later":
            st[stage] = "now"
    p = {"stage": stage, "done": done, "total": total, "t": time.time(), "pid": os.getpid(), "started": _P["started"],
         "stages": st, "files": _P.get("files"), "paused": _P.get("paused", False), **kw}
    if _P.get("memwait"):   # waiting for memory (memory_wait): the reason the numbers stand still
        p["memwait"] = _P["memwait"]
    try:
        f = Path(d, "progress.json"); tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(p), encoding="utf-8"); os.replace(tmp, f)   # a reader never sees half a file
    except OSError:
        pass


_TICK = {}


def tick(stage, done, total, every=0.7, **kw):
    """progress at most every `every` seconds, with the rate and the minutes left of this stage. The app reads the file
    every 0.7 s: counts written per 200 files, per 256 pieces or per folder left the first index looking frozen for a
    minute or more on slow PCs (11/10: Windows and Mac, the name scan of a whole home folder is one folder)"""
    now = time.time()
    t = _TICK.get(stage)
    if t is None or done < t["done0"]:
        t = _TICK[stage] = {"t0": now, "done0": done, "last": 0.0}
    if now - t["last"] < every and done != total:
        return
    t["last"] = now
    rate = (done - t["done0"]) / max(now - t["t0"], 1e-6)
    if total and rate > 0 and now - t["t0"] >= 3:   # a few seconds of work before guessing the rest
        kw.setdefault("minutes_left", round((total - done) / rate / 60, 1))
    progress(stage, done, total, rate=round(rate, 1), **kw)


MEM_LOW_GB = 0.8     # free memory below this: the indexer lets go of what it can and waits (the engine and the person first)
MEM_BACK_GB = 2.0    # ... and goes on once this much is free again
# on 12 GB or less, 2 GB free never came back while the engine ran: an 8 GB laptop has about 1.5 GB free with the engine up
# (cli._lane2), and the meaning stage waited at 0% for about 4 minutes, going on only when the engine stopped (#198)
MEM_BACK_SMALL_GB = 1.2
SMALL_RAM_GB = 12.5  # a PC sold as 12 GB reports a little less
MEM_WAIT_MAX = 900   # never waits longer than this at a time: memory another program keeps forever does not stop indexing
MEM_WAIT_LIGHT = 120  # ... and not this long when the indexer held no model: its waiting frees nothing of its own
MEM_BEAT = 30        # a waiting indexer says so in progress.json this often (the page drops a line older than 15 minutes)
_RELEASE = []        # what the indexer can let go of under memory pressure (its own encoder); taken up again when needed.
# Each returns True when it let go of something it held


def mem_free_gb():
    """memory free now in GB (None when it cannot be read)"""
    try:
        from governor import mem_available_gb
        return mem_available_gb()
    except Exception:   # noqa: BLE001
        return None


def ram_gb():
    """this PC's memory in GB (None when it cannot be read)"""
    try:
        from governor import ram_total_gb
        return ram_total_gb()
    except Exception:   # noqa: BLE001
        return None


def mem_back_gb():
    """free memory at which a waiting indexer goes on: FINDR_MEM_BACK_GB, else 1.2 GB on PCs with 12 GB or less, else 2 GB.
    Never at or below MEM_LOW_GB (it would stop again at once)"""
    try:
        v = float(os.environ.get("FINDR_MEM_BACK_GB") or 0)
    except ValueError:
        v = 0
    if v <= 0:
        ram = ram_gb()
        v = MEM_BACK_SMALL_GB if ram and ram < SMALL_RAM_GB else MEM_BACK_GB
    return max(v, MEM_LOW_GB + 0.1)


def memory_wait():
    """when the PC is nearly out of memory: let go of the indexer's own model, and wait until memory is back. Holding alone
    frees nothing (a held process keeps what it holds, measured in a 3 GB limit); letting go of the model does, and not
    reading more files keeps the indexer from pushing the PC into paging while the person works (field, 16 GB laptop).
    The wait is in progress.json (memwait), so the app and the page can say why indexing does not move"""
    free = mem_free_gb()
    if free is None or free >= MEM_LOW_GB:
        return
    held = False
    for f in list(_RELEASE):
        try:
            held = bool(f()) or held
        except Exception:   # noqa: BLE001
            pass
    import gc
    gc.collect()
    back = mem_back_gb()
    most = MEM_WAIT_MAX if held else MEM_WAIT_LIGHT
    print(f"FINDR_MEMWAIT {free:.1f} GB free: indexing waits for memory (until {back:.1f} GB, at most {most}s)", flush=True)
    t0 = beat = time.time()
    _P["memwait"] = {"free_gb": round(free, 2), "back_gb": round(back, 2), "since": t0, "max_s": most}
    _repeat_progress()
    try:
        while time.time() - t0 < most:
            time.sleep(5)
            free = mem_free_gb()
            if free is None or free >= back:
                break
            if time.time() - beat >= MEM_BEAT:
                beat = time.time(); _P["memwait"]["free_gb"] = round(free, 2); _repeat_progress()
    finally:
        _P.pop("memwait", None)
        _repeat_progress()
    print(f"FINDR_MEMBACK {free if free is not None else 0:.1f} GB free after {time.time() - t0:.0f}s", flush=True)


PAUSE_MAX = 3600   # a pause the person forgot about lapses after an hour
PAUSE_BEAT = 60    # a paused indexer says it is still here this often (the page drops a progress line after 15 minutes)


class GiveBack(Exception):
    """this run stops here and keeps what it stored (committed by main): the person is back, the charger came out, or the
    disk is nearly full. The app runs it again later; nothing read so far is read again"""

    def __init__(self, why, detail=""):
        super().__init__(why)
        self.why, self.detail = why, detail


GIVE_BACK_CODE = {"input": 7, "battery": 7, "disk": 8}   # exit codes: 7 gave the PC back, 8 the disk is nearly full
DISK_MIN_GB = float(os.environ.get("FINDR_DISK_MIN_GB") or 1.0)   # free space the index never takes (the PC needs some)
DISK_EVERY = 10   # seconds between looks at the free space
_GB = {"t0": time.time(), "looked": 0.0, "disk": 0.0, "db": None}


def disk_free_gb(path=None):
    import shutil
    try:
        return shutil.disk_usage(path or _GB["db"] or ".").free / 2 ** 30
    except OSError:
        return None


def disk_check(now=None):
    """stop cleanly before the drive that holds the index is full: an index write on a full drive failed in the middle of
    a run, and every later run read the same files again and failed the same way"""
    now = time.time() if now is None else now
    if now - _GB["disk"] < DISK_EVERY:
        return
    _GB["disk"] = now
    free = disk_free_gb()
    if free is not None and free < DISK_MIN_GB:
        raise GiveBack("disk", f"{free:.2f} GB free")


def give_back_check(now=None):
    """the app's background runs (refresh, deep run) give the PC back: FINDR_STOP_ON_INPUT = seconds of recent keyboard or
    mouse use that count as 'the person is back', FINDR_STOP_ON_BATTERY=1 stops when the charger comes out, and
    FINDR_STOP_GRACE = seconds of work always done first (a refresh with a few new files finishes). The person's own runs
    set none of them. A deep run started in idle time (a video lecture, no input for 5 minutes) went on for hours after
    the person came back (field, 16 GB laptop)"""
    within = float(os.environ.get("FINDR_STOP_ON_INPUT") or 0)
    battery = os.environ.get("FINDR_STOP_ON_BATTERY") == "1"
    if not within and not battery:
        return
    now = time.time() if now is None else now
    if now - _GB["looked"] < LOOK_EVERY or now - _GB["t0"] < float(os.environ.get("FINDR_STOP_GRACE") or 0):
        return
    _GB["looked"] = now
    if within and person_is_back(within):
        raise GiveBack("input")
    if battery:
        try:
            from governor import on_battery
        except ImportError:
            return
        if on_battery():
            raise GiveBack("battery")


def pause_wait(give_back=True):
    """hold while the person paused indexing (the app or the search page writes 'pause' in the data folder), and while the
    PC is nearly out of memory (memory_wait). Raises GiveBack where this run should stop (give_back_check, disk_check);
    give_back=False where only the pause and memory count (the scan of names: cheap, and new files show by name)"""
    memory_wait()
    disk_check()
    if give_back:
        give_back_check()
    d = os.environ.get("FINDR_DATA")
    if not d:
        return
    f = Path(d, "pause")
    held = False
    beat = time.time()
    while f.exists():
        try:
            if time.time() - f.stat().st_mtime > PAUSE_MAX:
                f.unlink(); break
        except OSError:
            break
        if not held:
            held = True; _P["paused"] = True
            print("FINDR_PAUSED", flush=True); _repeat_progress()
        elif time.time() - beat > PAUSE_BEAT:
            # still here while paused: the search page (and, on Windows, the engine) take a progress line older than
            # 15 minutes for a stopped indexer, and showed '모두 준비됐어요' with no way to resume (field, Windows)
            beat = time.time(); _repeat_progress()
        time.sleep(1)
    if held:
        _P["paused"] = False; print("FINDR_RESUMED", flush=True); _repeat_progress()


def _repeat_progress():
    d = os.environ.get("FINDR_DATA")
    try:
        last = json.loads(Path(d, "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        last = {"stage": "names", "done": 0, "total": 0}
    kw = {k: v for k, v in last.items() if k not in ("stage", "done", "total", "t", "pid", "started", "stages", "files", "paused", "memwait")}
    progress(last.get("stage", "names"), last.get("done", 0), last.get("total", 0), **kw)


def priority(path, ext, mtime, now):
    """higher = read sooner: recent first; documents and the user's document folders ahead; machine logs and bulk data behind"""
    age = (now - mtime) / 86400
    bonus = 0
    if ext in DOC or ext in (".hwp", ".doc", ".ppt", ".xls", ".md", ".txt", ".html"):
        bonus += 60
    if "/Documents/" in path or "/GoogleDrive/" in path:
        bonus += 30
    if ext in DATA:
        bonus -= 60
    return bonus - age


def cloud_only(st):
    """a cloud placeholder (Windows: OneDrive 'online-only' and similar): its content is not on this PC"""
    a = getattr(st, "st_file_attributes", 0)
    return bool(a & (0x00400000 | 0x00040000 | 0x00001000))   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE


def cloud_only_path(s):
    try:
        return cloud_only(os.stat(_long(s)))
    except OSError:
        return True   # cannot even stat it: do not try to read it


def like_pre(pre):
    """a folder prefix for LIKE with its own % and _ taken literally (ESCAPE '\\'): '~/my_docs' claimed '~/my-docs'"""
    return pre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


_LOCK = None


def one_indexer(db):
    """one indexer per index: a second `findr index` failed on the unique path or on a locked database. An OS lock on a file
    next to the index, released by the system when the process ends, so a killed run never blocks the next one."""
    global _LOCK
    path = str(Path(db).expanduser()) + ".lock"
    if _LOCK is not None and not _LOCK.closed and _LOCK.name == path:
        return True   # this process already holds it (main() run twice in one process)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCK = f
    return True


def _read_one(job):
    """text of one file; None when it could not be opened now (locked by another program): tried again next time"""
    s, lim = job
    try:
        return extract(Path(s), lim) or ""
    except (PermissionError, BlockingIOError):
        return None
    except OSError as e:
        return None if getattr(e, "winerror", None) in (32, 33) else ""   # sharing / lock violation on Windows
    except Exception:
        return ""


READER_GB = 0.35   # memory one reader process may take at its peak (parsers loaded, one large document open)


def read_workers():
    """reader processes for this run: FINDR_READ_WORKERS when set; else one per core less one, at most 8, no more than the
    run's thread share (FINDR_THREADS: the app gives indexing half the cores) and no more than free memory allows. A 16 GB
    laptop with a video and a browser open froze while the first index ran 8 readers beside the engine (field, 11-13)"""
    if os.environ.get("FINDR_READ_WORKERS"):
        return max(1, int(os.environ["FINDR_READ_WORKERS"]))
    n = max(1, min(8, (os.cpu_count() or 2) - 1))
    if os.environ.get("FINDR_THREADS"):
        n = min(n, max(1, int(os.environ["FINDR_THREADS"])))
    free = mem_free_gb()
    if free is not None:   # leave 2 GB for the person and the engine; what is left decides how many readers fit
        n = min(n, max(1, int((free - 2.0) / READER_GB)))
    return n


def read_all(jobs):
    """text of each (path, limit) in order; several worker processes when there is enough to read (read_workers)"""
    n = read_workers()
    if n <= 1 or len(jobs) < 16:
        yield from map(_read_one, jobs); return
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=n) as ex:
            # a few files ahead of the one being stored, not all of them: Executor.map submits every job at once, and the
            # texts read ahead (up to 2 MB each in a deep run) piled up in memory while storing was slower or paused
            ahead, it = deque(), iter(jobs)
            for j in it:
                ahead.append(ex.submit(_read_one, j))
                if len(ahead) >= n * 3:
                    break
            try:
                while ahead:
                    r = ahead.popleft().result()   # results come back in the given (priority) order
                    nxt = next(it, None)
                    if nxt is not None:
                        ahead.append(ex.submit(_read_one, nxt))
                    done += 1
                    yield r
            finally:   # the run stopped early (the person is back, the disk is full): files not started are not read
                for fu in ahead:
                    fu.cancel()
    except Exception:   # a worker died (out of memory, a reader crashed), or no worker processes on this platform
        # the chunk that was being read stays pending (None) for the next run: it may be what crashed the worker;
        # the rest is read here, each text still with its own file
        skip = min(len(jobs), done + 4) if done else 0
        yield from (None for _ in range(done, skip))
        yield from map(_read_one, jobs[skip:])


# pieces per MB of documents read in full, by kind (a real PC's index, 829 documents, 11/13); pdf between that PC (3.7, many
# scans and slides) and a folder of text-heavy reports (82). For a deep-run estimate on a PC with fewer than 5 of a kind read
DEEP_PPMB = {".pdf": 17, ".docx": 4.7, ".doc": 14, ".hwp": 14, ".hwpx": 14, ".pptx": 0.6, ".ppt": 0.6, ".xlsx": 375, ".xls": 375,
             ".html": 250, ".htm": 250}


def deep_left(con, cap=None):
    """the pieces a deep run will embed, and in how many files: (pieces, files). #67-1: the first pass said 127,674 pieces
    left, the deep run embedded 725,756 on a Mac. What it does:
    - files read in full: their pieces past the opening `cap` (FIRST_PIECES) that have no vector yet
    - files read in part (pending=2): read again from the start, so all their pieces, the ones there now included. Their
      size tells how many: plain text at the pieces per byte of what was read from that file (nothing read from the
      opening, nothing more); a document read through a parser (pdf, hwp...) at the pieces per byte of files of that kind
      read in full here (DEEP_PPMB when fewer than 5, else all kinds together)
    - files not read yet (pending=1): the same, from their size"""
    cap = int(os.environ.get("FINDR_FIRST_PIECES") or 8) if cap is None else cap
    work, files = 0, set()
    for fid, k in con.execute("SELECT c.file, count(*) FROM chunks c JOIN files f ON f.id=c.file WHERE c.emb IS NULL "
                              "AND f.tier>0 AND COALESCE(f.pending,0)=0 AND c.n >= ? GROUP BY c.file", (cap,)):
        work += k; files.add(fid)
    per = {}   # ext -> [pieces, bytes, files] of files read in full
    for ext, n_, b_ in con.execute("""SELECT f.ext, count(c.id), f.size FROM files f JOIN chunks c ON c.file=f.id
                                       WHERE f.tier=1 AND COALESCE(f.pending,0)=0 AND f.size>0 AND COALESCE(c.text,'')!=''
                                       GROUP BY f.id"""):
        a_ = per.setdefault((ext or "").lower(), [0, 0, 0]); a_[0] += n_; a_[1] += b_; a_[2] += 1
    nb = sum(v[1] for v in per.values())
    every = sum(v[0] for v in per.values()) / nb if nb else 0.0
    rate = {e: v[0] / v[1] for e, v in per.items() if v[2] >= 5 and v[1]}
    for fid, ext, size, pend, have, txt, got in con.execute(
            """SELECT f.id, f.ext, f.size, f.pending, count(c.id), count(NULLIF(c.text, '')), COALESCE(sum(length(CAST(c.text AS BLOB))), 0)
               FROM files f LEFT JOIN chunks c ON c.file=f.id WHERE f.tier>0 AND f.pending IN (1,2) GROUP BY f.id"""):
        e_ = (ext or "").lower()
        if pend == 2 and e_ not in RICH:
            whole = txt * (size or 0) / got if got else 0   # this file's own pieces per byte
        else:
            whole = (size or 0) * rate.get(e_, DEEP_PPMB[e_] / 1e6 if e_ in DEEP_PPMB else every)
        work += max(have, min(2000, round(whole)), 1); files.add(fid)
    return work, len(files)


def read_wave(con, root, a):
    """read the content of pending files under this root, best first, within this wave's age and size limits"""
    now = time.time(); pre = root.as_posix().rstrip("/") + "/"
    first = int(os.environ.get("FINDR_FIRST_PIECES") or 0) > 0
    # first pass: pending=1 (never read); the deep run also finishes the files read only in part (pending=2)
    rows = con.execute(f"SELECT id, path, ext, mtime, tier FROM files WHERE pending IN ({'1' if first else '1,2'}) AND path LIKE ? ESCAPE '\\'",
                       (like_pre(pre),)).fetchall()
    if a.max_age_days:
        rows = [r for r in rows if now - r[3] <= a.max_age_days * 86400]
    rows.sort(key=lambda r: -priority(r[1], r[2], r[3], now))
    if a.limit:
        rows = rows[: a.limit]
    t0 = time.time()
    full = lambda ext, tr: 1500 if tr == 2 else DATA_READ if ext in DATA else MAXREAD
    jobs = [(s, min(GIST, full(ext, tr)) if first else full(ext, tr)) for _, s, ext, _, tr in rows]
    locked = 0
    for i, ((fid, s, ext, mtime, tr), txt) in enumerate(zip(rows, read_all(jobs))):
        pause_wait()
        if txt is None:   # open in another program right now: stays pending, read on the next run
            locked += 1
            continue
        p = Path(s)
        cred = bool(txt) and secret_text(p.name, txt)
        cs = [""] if cred else chunks(txt)[: (1 if tr == 2 else 2000)] or [""]   # a password export under any name: its name only
        con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=? AND n>=0)", (fid,))
        con.execute("DELETE FROM chunks WHERE file=? AND n>=0", (fid,))
        if cred:
            con.execute("UPDATE files SET tier=0 WHERE id=?", (fid,))
        rel = s[len(root.as_posix()):]
        for n, c in enumerate(cs):
            cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, n, c)).lastrowid
            if not cred:   # a secret file is not in the word index at all (fts_no_secrets)
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p.name), nfc(rel), nfc(c)))
        part = not cred and first and tr == 1 and ext not in DATA and (len(txt) >= GIST - 64 or (ext not in RICH and _size(s) > GIST))   # cut at the gist
        # (plain text is read by bytes: Korean takes 3 per character, so the file size tells): the deep run reads the rest
        con.execute("UPDATE files SET pending=? WHERE id=?", (2 if part else 0, fid))
        if (i + 1) % 200 == 0:
            con.commit(); print(f"  read {i + 1}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
        tick("content", i + 1, len(rows), folder=root.name)
    con.commit()
    left = con.execute("SELECT count(*) FROM files WHERE pending IN (1,2) AND path LIKE ? ESCAPE '\\'", (like_pre(pre),)).fetchone()[0]
    print(f"FINDR_READ {len(rows)} files in {time.time() - t0:.0f}s, still pending {left}" + (f" (in use by another program: {locked})" if locked else ""))


SHOTLIKE = re.compile(r"screenshot|screen ?shot|스크린샷|캡처|캡쳐|화면 ?캡|capture|snip|clip|scan|스캔|영수증|receipt"
                      r"|등록증|증명|확인서|신분증|명함|계약서|통장|사본|진단서|청구서|여권|면허|certificate|license|passport|contract|invoice", re.I)
# (images named like documents: a photographed certificate or contract is a document; its text is read on the first pass)
_OCR = None


OCR_SIDE = int(os.environ.get("FINDR_OCR_SIDE") or 2400)   # longest side of a page image given to OCR (pixels)


_ENGINE = None


def ocr_engine():
    """the text recognizer for this system: 'vision' (macOS built-in, Korean in accurate mode), 'windows' (Windows' own OCR,
    when its Korean recognizer is installed), else 'easyocr' (NVIDIA or Apple GPU, or the CPU). FINDR_OCR_ENGINE overrides.
    On an M3, 6 Korean slide pages: Vision 0.6 s/page, EasyOCR on the Apple GPU 4.3 s, on the CPU 22.8 s; Vision also misread less.
    On 12 synthetic Korean/English A4 pages, 4 CPU cores: EasyOCR 96% of characters at 14.6 s/page."""
    global _ENGINE
    if _ENGINE:
        return _ENGINE
    want = (os.environ.get("FINDR_OCR_ENGINE") or "").lower()
    if want in ("vision", "windows", "easyocr"):
        _ENGINE = want; return want
    _ENGINE = "easyocr"
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401  (pyobjc-framework-Vision)
            _ENGINE = "vision"
        except ImportError:
            pass
    elif sys.platform == "win32" and _windows_ocr() is not None:
        _ENGINE = "windows"
    if _ENGINE == "easyocr":
        import importlib.util
        if importlib.util.find_spec("easyocr") is None:
            _ENGINE = "none"   # no recognizer installed here (#31): scans stay findable by name
    return _ENGINE


def missing_parts():
    """readers this run will go without, each with what it costs (#31: after a git pull nothing said so)"""
    import importlib.util
    out = []
    if ocr_engine() == "none":
        out.append("easyocr (스캔·스크린샷 글자)")
    for mod, what in (("pypdfium2", "빠른 PDF 읽기"), ("pillow_heif", "아이폰 사진 HEIC"), ("python_calamine", "xls·ods 표")):
        if importlib.util.find_spec(mod) is None:
            out.append(f"{mod} ({what})")
    return out


_WIN = []


def _windows_ocr():
    """Windows' own recognizer for Korean (Windows 10/11 with the Korean OCR feature; Korean Windows has it), or None.
    Without it: Settings > Time & language > Language > Korean > Optional features, or
    Add-WindowsCapability -Online -Name "Language.OCR~~~ko-KR~0.0.1.0" (administrator)."""
    if not _WIN:
        eng = None
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
            eng = OcrEngine.try_create_from_language(Language("ko")) if OcrEngine.is_language_supported(Language("ko")) else None
        except Exception:
            eng = None
        _WIN.append(eng)
    return _WIN[0]


def _windows_lines(img):
    """Windows OCR (Korean recognizer; it also reads Latin letters and digits), lines top to bottom"""
    import asyncio
    import numpy as np
    from PIL import Image
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    eng = _windows_ocr()
    im = Image.fromarray(img) if not isinstance(img, (str, Path)) else Image.open(img)
    im = im.convert("RGB")
    side = max(1, int(getattr(eng, "max_image_dimension", 0) or 2600))   # larger images are refused
    if max(im.size) > side:
        im.thumbnail((side, side))
    a = np.asarray(im.convert("RGBA"))[..., [2, 1, 0, 3]].copy()   # BGRA, as the recognizer takes it
    w = DataWriter(); w.write_bytes(a.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(w.detach_buffer(), BitmapPixelFormat.BGRA8, im.width, im.height)

    async def go():
        return await eng.recognize_async(bmp)
    res = asyncio.run(go())
    return [ln.text for ln in res.lines if ln.text.strip()]


def ocr_reader():
    """EasyOCR (Korean + English), loaded once; its models live in findr's model folder. Uses NVIDIA or the Apple GPU when present."""
    global _OCR
    if _OCR is None:
        import easyocr, torch
        d = Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "ocr"
        d.mkdir(parents=True, exist_ok=True)
        mps = getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()
        _OCR = easyocr.Reader(["ko", "en"], gpu=torch.cuda.is_available() or mps, model_storage_directory=str(d), verbose=False)   # Apple GPU too (#8)
    return _OCR


def _vision_lines(img):
    """macOS Vision text recognition (accurate, Korean + English), lines top to bottom"""
    import io
    import Vision
    from Foundation import NSData
    from PIL import Image
    im = Image.fromarray(img) if not isinstance(img, (str, Path)) else Image.open(img).convert("RGB")
    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=92); b = buf.getvalue()
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)   # accurate: the level that has Korean
    # language correction roughly doubles the time (M3: 0.45 -> 0.23 s/page without it) and fixes Korean words
    # (장연 -> 장면, 명렁 -> 명령); kept on unless FINDR_OCR_FAST=1
    req.setRecognitionLanguages_(["ko-KR", "en-US"]); req.setUsesLanguageCorrection_(os.environ.get("FINDR_OCR_FAST") != "1")
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(b, len(b)), None)
    ok, err = h.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(str(err))
    obs = []
    for o in req.results() or []:
        c = o.topCandidates_(1)
        if c and c[0].string().strip():
            bb = o.boundingBox()   # normalised, origin bottom-left
            obs.append((-round(bb.origin.y + bb.size.height, 2), bb.origin.x, c[0].string()))
    return [t for _, _, t in sorted(obs)]


def ocr_lines(img):
    """text lines of one image (a path or an RGB array), top to bottom"""
    try:
        if ocr_engine() == "vision":
            return _vision_lines(img)
        if ocr_engine() == "windows":
            return _windows_lines(img)
    except Exception:
        pass   # the system recognizer failed on this image: EasyOCR below
    try:
        return [t for t in ocr_reader().readtext(img, detail=0, paragraph=True) if t.strip()]
    except Exception:
        return []


def _page_scale(page, side=None):
    """2x for normal pages, less for big ones so the image's longest side stays near OCR_SIDE (a 3670-pt slide was 7339 px)"""
    w, h = page.get_size()
    return max(0.5, min(2.0, (side or OCR_SIDE) / max(w, h, 1)))


OCR_FIRST_PAGES = int(os.environ.get("FINDR_OCR_FIRST_PAGES") or 3)


_SAID = []


def _gpu_present():
    try:
        import torch
        return torch.cuda.is_available() or (getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available())
    except Exception:
        return False
OCR_PARTIAL = 3   # files.ocr: 1 = read, 3 = only the first pages read (a deep run reads the rest)


def _file_sig(path):
    """content fingerprint for finding copies ('견적서 (1).pdf' next to '견적서.pdf'): sha1 of the whole file"""
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _ocr_copy_of(con, fid, path, size, deep):
    """an identical file that was already OCR'd (same size, same bytes): (its chunk texts, its ocr state), or None"""
    want = (1,) if deep else (1, OCR_PARTIAL)
    rows = con.execute(f"SELECT id, path, ocr FROM files WHERE size=? AND id<>? AND ocr IN ({','.join('?' * len(want))})",
                       (size, fid, *want)).fetchall()
    if not rows:
        return None
    try:
        sig = _file_sig(path)
    except OSError:
        return None
    for oid, opath, ost in rows:
        try:
            if _file_sig(opath) != sig:
                continue
        except OSError:
            continue
        texts = [t for (t,) in con.execute("SELECT text FROM chunks WHERE file=? AND n>=0 ORDER BY n", (oid,))]
        return texts, ost
    return None


def photos_to_read(rows, deep, engine):
    """which photos the text recognizer reads now. rows: (id, path), newest first. A deep run reads them all. A first pass
    reads screenshot-like names, and with the system's own recognizer (macOS Vision, Windows OCR) also the newest
    FINDR_OCR_PHOTOS photos (300): on a Mac Air (11/9) half of 50 random photos in the person's own folders carried text worth
    finding (screenshots, product pages, slides, posters, notes) and Vision read one in 0.17 s, yet 1 of 1,443 had any text
    in the index, because the deep run that reads the rest had not come"""
    if deep:
        return list(rows)
    first = [r for r in rows if SHOTLIKE.search(r[1])]
    if engine in ("vision", "windows"):
        n = int(os.environ.get("FINDR_OCR_PHOTOS") or 300)
        have = {r[0] for r in first}
        first += [r for r in rows if r[0] not in have][:max(0, n)]
    return first


def ocr_wave(con, root, first_pages=None, deep_pages=None):
    """scanned PDFs and screenshots now; every photo on a deep run. Each file is read once (files.ocr).
    First pass: the first OCR_FIRST_PAGES pages of a PDF (files.ocr = 3 when pages are left); a deep run reads the rest.
    A copy of a file already read gets that file's text instead of a second OCR."""
    deep = int(os.environ.get("FINDR_FIRST_PIECES") or 0) == 0
    deep_pages = deep_pages or int(os.environ.get("FINDR_OCR_DEEP_PAGES") or 50)   # indexing stays bounded; one file asked for is read further (deepread.py)
    first_pages, side = first_pages or OCR_FIRST_PAGES, OCR_SIDE
    if ocr_engine() == "none":
        return   # nothing to read with (FINDR_WARN at the start of the run says what to install)
    if ocr_engine() == "easyocr" and not _gpu_present() and not deep and os.environ.get("FINDR_OCR_FIRST") != "1":
        if not _SAID:
            _SAID.append(1); print("FINDR_OCR later: scans are read during idle time on this PC (no GPU or system text recognizer)", flush=True)
            _P["stages"]["ocr"] = "later"
        return   # first index: speed first; the deep run reads them
    if ocr_engine() == "easyocr" and not _gpu_present():
        # EasyOCR on a CPU, for search (words that find the file, not a faithful copy). 12 synthetic A4 pages, 4 cores,
        # share of words found / s per page:  2400 px 89-91% 9-15 s,  1600 px 90% 4.1-4.4 s,  1200 px 85-88% 2.6-2.9 s,
        # 1000 px 72-87% 2 s (small print falls off). First pass: page 1 at 1200 px; the deep run: the rest at 1600 px.
        first_pages = int(os.environ.get("FINDR_OCR_FIRST_PAGES") or 1)
        side = int(os.environ.get("FINDR_OCR_SIDE") or (1600 if deep else 1200))
    if ocr_engine() == "easyocr":
        try:
            import easyocr  # noqa: F401
        except ImportError:
            return
    if "ocr" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN ocr INT DEFAULT 0")
    pre = root.as_posix().rstrip("/") + "/"
    pdfs = [r for r in con.execute("""SELECT f.id, f.path FROM files f WHERE f.ext='.pdf' AND f.tier=1 AND f.path LIKE ? ESCAPE '\\'
              AND ((COALESCE(f.ocr,0)=0 AND (SELECT COALESCE(sum(length(c.text)),0) FROM chunks c WHERE c.file=f.id) < 200)
                   OR (? AND f.ocr=?))
              ORDER BY f.mtime DESC""", (like_pre(pre), 1 if deep else 0, OCR_PARTIAL))]
    rows = [(i, s) for i, s, e, z in con.execute("SELECT id, path, ext, size FROM files WHERE tier=3 AND COALESCE(ocr,0)=0 AND path LIKE ? ESCAPE '\\' AND size < 30000000 "
                                                 "ORDER BY mtime DESC", (like_pre(pre),)) if (e or "").lower() in PHOTO_EXT
            and not cloud_only_path(s) and not asset_like(s, z)]   # an online-only photo is not downloaded for OCR (a deep run fetched whole photo libraries)
    imgs = photos_to_read(rows, deep, ocr_engine())
    if not pdfs and not imgs:
        return
    t0, got, chars, copied = time.time(), 0, 0, 0
    heif_on()
    items = pdfs + imgs
    for k, (fid, s) in enumerate(items):
        pause_wait()
        lines, state, texts = [], 1, None
        tick("ocr", k, len(items), folder=root.name)
        try:
            size = os.path.getsize(s)
        except OSError:
            size = -1
        same = _ocr_copy_of(con, fid, s, size, deep) if size > 0 else None
        if same:
            texts, state = same; copied += 1
        elif s.lower().endswith(".pdf"):
            try:
                import numpy as _np, pypdfium2 as pdfium
                doc = pdfium.PdfDocument(s); end = time.time() + 3 * FILE_SECONDS   # reading a scan is slower than reading text
                upto = min(len(doc), deep_pages if deep else first_pages)
                if not deep and len(doc) > upto:
                    state = OCR_PARTIAL   # pages left for the deep run
                for i in range(upto):
                    if time.time() > end:
                        break
                    pg = doc[i]
                    lines += ocr_lines(_np.array(pg.render(scale=_page_scale(pg, side)).to_pil().convert("RGB")))
                doc.close()
            except Exception:
                pass
        else:
            try:
                from PIL import Image
                import numpy as _np
                im = Image.open(s).convert("RGB"); im.thumbnail((side, side))
                lines = ocr_lines(_np.array(im))
            except Exception:
                lines = []
        txt = "\n".join(texts if texts is not None else lines)
        if len(re.sub(r"\s", "", txt)) >= 8:   # real text, not a stray mark
            p_ = Path(s); rel = s[len(root.as_posix()):]
            con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=? AND n>=0)", (fid,))
            con.execute("DELETE FROM chunks WHERE file=? AND n>=0", (fid,))
            for n, c in enumerate(([mask(t) for t in texts] if texts is not None else chunks(txt))[:2000] or [""]):
                cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, n, c)).lastrowid
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p_.name), nfc(rel), nfc(c)))
            got += 1; chars += len(txt)
        con.execute("UPDATE files SET ocr=? WHERE id=?", (state, fid))
        con.commit()   # each file: progress is visible and an interrupted run keeps what it read
    con.commit()
    print(f"FINDR_OCR {len(pdfs)} scanned PDFs + {len(imgs)} images read, text found in {got} ({chars:,} chars), "
          f"{copied} copies of files already read, {time.time() - t0:.0f}s", flush=True)


def fts_no_secrets(con):
    """secret files (tier 0) out of the word index (fts): they are never shown, and their rows still counted in its ranking
    statistics (bm25: how rare a word is), which a search that leaves them out could be ordered by. Rows an older index
    keeps for them (their name and folder) leave here; new ones are never written"""
    n = con.execute("DELETE FROM fts WHERE rowid IN (SELECT c.id FROM chunks c JOIN files f ON f.id = c.file WHERE f.tier = 0)").rowcount
    if n > 0:
        print(f"FINDR_FTS_SECRET {n} rows of secret files left the word index", flush=True)
    return n


def remask(con):
    """every run: any stored chunk that still holds a key-like value is masked, re-indexed and re-embedded.
    Triggers note every chunk whose text is written (inserted, or filled in place by a later stage); only those are looked at,
    after one full look when the triggers are new or KEYLIKE changed. A run with no change scanned 920k chunks for 58 s (#67)"""
    import hashlib
    hit = 0
    con.execute("CREATE TABLE IF NOT EXISTS mask_todo(id INTEGER PRIMARY KEY)")
    con.execute("CREATE TRIGGER IF NOT EXISTS mask_ins AFTER INSERT ON chunks BEGIN INSERT OR IGNORE INTO mask_todo VALUES(NEW.id); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS mask_upd AFTER UPDATE OF text ON chunks BEGIN INSERT OR IGNORE INTO mask_todo VALUES(NEW.id); END")
    pat = hashlib.sha1((KEYLIKE.pattern + "|" + "|".join(KEYLIKE_HINTS)).encode()).hexdigest()[:16]
    seen = con.execute("SELECT value FROM meta WHERE key='mask_pattern'").fetchone()
    w = " OR ".join(["c.text LIKE ?"] * len(KEYLIKE_HINTS))
    # CROSS JOIN keeps mask_todo outer: with a plain JOIN SQLite scanned every chunk (2.6 GB, ~5 s) to find an empty todo list
    src = "chunks c" if not seen or seen[0] != pat else "mask_todo t CROSS JOIN chunks c ON c.id = t.id"
    rows = con.execute(f"SELECT c.id, c.file, c.text FROM {src} WHERE {w}", [f"%{h}%" for h in KEYLIKE_HINTS]).fetchall()
    for cid, fid, txt in rows:
        if txt and KEYLIKE.search(txt):
            t = mask(txt); name, path, tr = con.execute("SELECT name, path, tier FROM files WHERE id=?", (fid,)).fetchone()
            con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
            if tr != 0:
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(name), nfc(path), t))
            con.execute("UPDATE chunks SET text=?, emb=NULL WHERE id=?", (t, cid)); hit += 1
    con.execute("DELETE FROM mask_todo")   # (the masking above re-noted its own rows: already clean)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('mask_pattern', ?)", (pat,))
    con.commit(); print(f"FINDR_MASK {hit} chunks masked ({'all' if src == 'chunks c' else 'new or changed'} chunks looked at)")


PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}


def asset_like(path, size):
    """an image that is not a photo: macOS '._' metadata files, and icons, sprites and logos (short side under 200 px).
    On a developer's Mac (11/9) 73 of 100 image files were such assets and a photo model read them as cards and logos;
    reading or embedding them only costs time. Only small files are opened (header only)"""
    name = os.path.basename(path or "")
    if name.startswith("._") or (size or 0) < 2048:
        return True
    if (size or 0) >= 150_000:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            return min(im.size) < 200
    except Exception:
        return False


def heif_on():
    """iPhone photos (HEIC/HEIF) open like any other image once pillow-heif is installed"""
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass


def taken_at(path):
    """when the photo was taken (EXIF DateTimeOriginal / DateTime), or None: the file time stays the fallback"""
    try:
        from PIL import Image
        heif_on()
        ex = Image.open(path).getexif()
        v = ex.get_ifd(0x8769).get(36867) or ex.get(306)
        return datetime.datetime.strptime(str(v).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp() if v else None
    except Exception:
        return None


DB_WAITS = (5, 15, 30, 60, 120)   # seconds between tries when another writer holds the index


def db_retry(con, fn, waits=DB_WAITS):
    """fn() (which writes and commits), tried again after a wait while the index is locked by another writer. The photo
    thread in the search engine writes next to the indexer: a remask or an upgrade switch held the lock past the 60 s
    timeout, and the thread ended for the rest of the session with no line about it"""
    for i in range(len(waits) + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if i == len(waits) or not any(w in str(e).lower() for w in ("locked", "busy")):
                raise
            try:
                con.rollback()
            except sqlite3.Error:
                pass
            print(f"FINDR_DB busy: trying again in {waits[i]} s", flush=True)
            time.sleep(waits[i])


def embed_photos(con, url, bs=16):
    """photo vectors for photos that have none yet, newest first; a photo the model cannot read stays searchable by name"""
    import base64, json as _j, urllib.request

    def tables():
        con.execute("CREATE TABLE IF NOT EXISTS photo_emb(file INTEGER PRIMARY KEY, emb BLOB, taken REAL)")
        if "taken" not in [r[1] for r in con.execute("PRAGMA table_info(photo_emb)")]:
            con.execute("ALTER TABLE photo_emb ADD COLUMN taken REAL")
        con.commit()
    db_retry(con, tables)
    rows = [(i, p) for i, p, e, z in con.execute("SELECT id, path, ext, size FROM files WHERE id NOT IN (SELECT file FROM photo_emb) ORDER BY mtime DESC")
            if (e or "").lower() in PHOTO_EXT and not asset_like(p, z)]   # icons and '._' files: no photo vector (found by name)
    t0, done = time.time(), 0
    for k in range(0, len(rows), bs):
        b = rows[k:k + bs]
        try:
            req = urllib.request.Request(url.rstrip("/") + "/photo", _j.dumps({"paths": [p for _, p in b]}).encode(), {"Content-Type": "application/json"})
            vecs = _j.loads(urllib.request.urlopen(req, timeout=600).read())["vecs"]
        except Exception as e:
            print(f"FINDR_PHOTO stopped: photo model not reachable ({type(e).__name__})"); break
        got = [(i, base64.b64decode(v), taken_at(p)) for (i, p), v in zip(b, vecs) if v]

        def store():
            con.executemany("INSERT OR REPLACE INTO photo_emb(file, emb, taken) VALUES(?,?,?)", got); con.commit()
        db_retry(con, store); done += len(got)
    print(f"FINDR_PHOTO {done}/{len(rows)} photos {time.time() - t0:.0f}s")


def ooxml_text(p, limit):
    """Word, Excel, PowerPoint, HWPX: text parts read in pieces, stopping once `limit` characters are in.
    A large workbook can expand to gigabytes of XML; reading it whole used to take all the memory and time."""
    import zipfile
    keys = ("word/document", "sharedStrings", "worksheets/", "slides/slide", "Contents/section")
    def order(n):   # body first; slides and sections in their own order (slide2 before slide10)
        m = re.search(r"(\d+)\.xml$", n)
        return (0 if ("document" in n or "sharedStrings" in n) else 1, n.rsplit("/", 1)[0], int(m.group(1)) if m else 0)
    z = zipfile.ZipFile(p); out, have = [], 0; end = time.time() + FILE_SECONDS
    try:   # the properties the saving program wrote (title, subject, keywords, author): first, they say what the file is
        core = z.read("docProps/core.xml").decode("utf-8", "ignore")
        tag = lambda t: (re.search(rf"<(?:\w+:)?{t}[^>]*>([^<]*)</", core) or [None, None])[1]
        props = props_line(tag("title"), tag("subject"), tag("keywords"), tag("creator"))
        if props:
            out.append(props); have += len(props)
    except KeyError:
        pass
    for n in sorted((n for n in z.namelist() if n.endswith(".xml") and any(k in n for k in keys)), key=order):
        with z.open(n) as f:
            tail = ""
            while have < limit and time.time() < end:
                b = f.read(1 << 20)
                if not b:
                    break
                txt = tail + b.decode("utf-8", "ignore")
                cut = txt.rfind(">") + 1   # a tag may continue in the next piece
                tail = txt[cut:]
                t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt[:cut]))
                if t.strip():
                    out.append(t); have += len(t)
        if have >= limit:
            break
    return re.sub(r"\s+", " ", " ".join(out)).strip()[:limit]


def _snappy(b):
    """raw snappy block (what Keynote's .iwa chunks hold)"""
    i = n = sh = 0
    while True:
        c = b[i]; i += 1; n |= (c & 0x7F) << sh; sh += 7
        if c < 0x80:
            break
    out = bytearray()
    while i < len(b):
        tag = b[i]; i += 1; t = tag & 3
        if t == 0:
            ln = tag >> 2
            if ln >= 60:
                k = ln - 59; ln = int.from_bytes(b[i:i + k], "little"); i += k
            ln += 1; out += b[i:i + ln]; i += ln
            continue
        if t == 1:
            ln = ((tag >> 2) & 7) + 4; off = ((tag >> 5) << 8) | b[i]; i += 1
        elif t == 2:
            ln = (tag >> 2) + 1; off = int.from_bytes(b[i:i + 2], "little"); i += 2
        else:
            ln = (tag >> 2) + 1; off = int.from_bytes(b[i:i + 4], "little"); i += 4
        if off >= ln:
            out += out[-off:len(out) - off + ln]
        else:   # overlapping copy
            for _ in range(ln):
                out.append(out[-off])
    return bytes(out)


def _iwa(data):
    """an .iwa file: chunks of [0x00][3-byte length][snappy block]"""
    out, i = [], 0
    while i + 4 <= len(data):
        ln = int.from_bytes(data[i + 1:i + 4], "little"); i += 4
        try:
            out.append(_snappy(data[i:i + ln]))
        except Exception:
            break
        i += ln
    return b"".join(out)


_WORDY = re.compile(r"[가-힣A-Za-z]")
_STYLEID = re.compile(r"(paragraph|character|list|shape|cell|table|chart|slide|text|caption|connection|drawable)s?style|^#?[a-z]+-\d+(-|$)|^!", re.I)


def _pb_strings(buf):
    """text in protobuf bytes: every length-delimited field whose bytes read as text (Keynote's slide text, notes, titles)"""
    out, i, n = [], 0, len(buf)
    while i < n - 2:
        if buf[i] & 7 == 2 and buf[i] >> 3:
            j, ln, sh = i + 1, 0, 0
            while j < n and sh < 35:
                c = buf[j]; j += 1; ln |= (c & 0x7F) << sh; sh += 7
                if c < 0x80:
                    break
            if 2 <= ln <= 20000 and j + ln <= n:
                try:
                    t = buf[j:j + ln].decode("utf-8")
                except UnicodeDecodeError:
                    t = None
                if t and _WORDY.search(t) and sum(ch.isprintable() or ch in "\n\t" for ch in t) >= 0.95 * len(t) \
                        and not re.fullmatch(r"[A-Za-z0-9_.\-/]+", t):   # identifiers, style and class names are not text
                    out.append(t); i = j + ln; continue
        i += 1
    return out


# iWork documents carry the locale's calendar and currency names and a style sheet: not what people search for
_IWORK_NOISE = re.compile(r"^(?:[일월화수목금토]요일|오전|오후|AM|PM|(?:Sun|Mon|Tues|Wednes|Thurs|Fri|Satur)day|"
                          r"(?:January|February|March|April|May|June|July|August|September|October|November|December)|"
                          r"\d{1,2}월|[A-Z]{2}[$¥€£₩]|사용자 설정 포맷.*|Office Theme.*)$")
_IWORK_SKIP = ("Stylesheet", "ViewState", "Metadata", "CalculationEngine", "AnnotationAuthorStorage")


def keynote_text(p, limit):
    """Apple Keynote: '09 and older keep XML (index.apxl); 6+ keep snappy-framed protobuf (Index/*.iwa, sometimes in Index.zip)"""
    import gzip, io, zipfile
    try:
        z = zipfile.ZipFile(p)
        names = z.namelist()
        for nm in names:
            if nm.endswith(("index.apxl", "index.apxl.gz")):
                raw = z.read(nm)
                raw = gzip.decompress(raw) if nm.endswith(".gz") else raw
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore"))).strip()[:limit]
        if "Index.zip" in names:
            z = zipfile.ZipFile(io.BytesIO(z.read("Index.zip"))); names = z.namelist()
        seen, out, size = set(), [], 0; end = time.time() + FILE_SECONDS
        iwork = Path(str(p)).suffix.lower() in (".pages", ".numbers")
        for nm in sorted(names, key=lambda x: (not x.startswith(("Index/Slide", "Index/Tables/")), x)):   # slides / tables first
            if time.time() > end:
                break
            if not (nm.startswith("Index/") and nm.endswith(".iwa")) or z.getinfo(nm).file_size > 50_000_000:
                continue
            if iwork and any(k in nm for k in _IWORK_SKIP):
                continue
            for t in _pb_strings(_iwa(z.read(nm))):
                t = t.strip()
                if len(_WORDY.findall(t)) < 2 or _STYLEID.search(t):   # style ids, symbols: not what people search for
                    continue
                if iwork and _IWORK_NOISE.match(t):
                    continue
                if t and t not in seen:
                    seen.add(t); out.append(t); size += len(t)
            if size >= limit:
                break
        return "\n".join(out)[:limit]
    except Exception:
        return ""


def pdf_props(p):
    """title, subject and keywords that the saving program wrote into the PDF ('' when none or generic)"""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p))
        try:
            m = doc.get_metadata_dict(skip_empty=True)
        finally:
            doc.close()
    except Exception:
        return ""
    return props_line(m.get("Title"), m.get("Subject"), m.get("Keywords"), m.get("Author"))


_GENERIC = re.compile(r"^(untitled|제목 ?없음|microsoft (word|powerpoint|excel).*|document\d*|문서\d*|슬라이드 ?\d*|presentation\d*|pdf|unknown|user|admin|owner)$", re.I)


def props_line(title=None, subject=None, keywords=None, author=None):
    """one line of document properties for the index, skipping the empty and the generic ('Microsoft Word - 문서1')"""
    out = []
    for k, v in (("제목", title), ("주제", subject), ("키워드", keywords), ("작성자", author)):
        v = re.sub(r"\s+", " ", str(v or "")).strip()[:200]
        if v and not _GENERIC.match(v) and not re.fullmatch(r"[\W_]*", v):
            out.append(f"{k}: {v}")
    return (" | ".join(out) + "\n") if out else ""


def pdf_text(p, limit):
    """pdftotext when it is installed (Linux; best layout), else pypdf (Windows, macOS: nothing extra to install)"""
    import shutil
    if shutil.which("pdftotext") and os.environ.get("FINDR_PDF") in (None, "", "pdftotext"):
        pages = "500" if limit > 100_000 else "12"   # a gist needs the first pages only
        out = subprocess.run(["pdftotext", "-layout", "-l", pages, str(p), "-"], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
        return (pdf_props(p) + out)[:limit]
    if os.environ.get("FINDR_PDF") != "pypdf":
        try:
            import pypdfium2 as pdfium   # Chrome's PDF engine: much faster than pure-Python parsing
            doc = pdfium.PdfDocument(str(p)); out, n = [], 0; end = time.time() + FILE_SECONDS
            try:
                for i in range(len(doc)):
                    if time.time() > end:
                        break
                    pg = doc[i]; tp = pg.get_textpage(); t = tp.get_text_range() or ""; tp.close(); pg.close()
                    out.append(t); n += len(t)
                    if n >= limit:
                        break
            finally:
                doc.close()
            return (pdf_props(p) + "\n".join(out))[:limit]
        except ImportError:
            pass
        except Exception:
            return ""
    try:
        from pypdf import PdfReader
        out, n = [], 0; end = time.time() + FILE_SECONDS
        for pg in PdfReader(str(p)).pages:
            if time.time() > end:
                break
            t = pg.extract_text() or ""; out.append(t); n += len(t)
            if n >= limit:
                break
        return "\n".join(out)[:limit]
    except Exception:
        return ""


def gone_check(con, roots, now=None):
    """files deleted (or moved away) since the last check leave the index without a walk of every folder: only the folders
    that hold indexed files are looked at, and only those whose time changed since the last check are listed (a folder's
    time moves when a file in it is added, removed or renamed). A folder that is not there while the root is: deleted with
    its files. Kept, as in a full run: a root that is not there (a drive unplugged), a folder that cannot be read, and a
    root that would lose more than half of its files at once (a bad mount, not a deletion), and a file that looks like one
    that arrived in a listed folder (same size, time and format: a move the next full run pairs). Returns the counts it prints"""
    import posixpath
    t_run = time.time()
    t0 = t_run if now is None else now   # the stamp kept for the next check (tests pin it)
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    last = float((con.execute("SELECT value FROM meta WHERE key='gone_at'").fetchone() or ["0"])[0])
    st_ = {"folders": 0, "listed": 0, "removed": 0, "kept": 0, "waiting": 0}
    for root in roots:
        if not os.path.isdir(_long(str(root))):
            print(f"FINDR_ROOT_MISSING {root} (kept as indexed)", flush=True)
            continue
        pre = root.as_posix().rstrip("/") + "/"
        rows = con.execute("SELECT id, path FROM files WHERE path LIKE ? ESCAPE '\\'", (like_pre(pre),)).fetchall()
        by_dir = {}
        for fid, p in rows:
            by_dir.setdefault(posixpath.dirname(p), []).append((fid, posixpath.basename(p)))
        gone, arrived = [], set()   # arrived: (size, whole seconds, format) of files new to listed folders
        for d, files in by_dir.items():
            st_["folders"] += 1
            try:
                if os.stat(_long(str(Path(d)))).st_mtime < last - 1:
                    continue   # nothing added, removed or renamed in it since the last check
                names = set(os.listdir(_long(str(Path(d)))))
            except FileNotFoundError:
                gone += [fid for fid, _ in files]   # the folder is gone and its root is here: deleted with it
                continue
            except OSError:
                continue   # cannot be read now (permission, a share that dropped): kept
            st_["listed"] += 1
            gone += [fid for fid, n in files if n not in names]
            for n in names - {n for _, n in files}:   # new here: maybe a file of the index that was moved or renamed
                try:
                    s_ = os.stat(_long(str(Path(d) / n)))
                except OSError:
                    continue
                if _stat.S_ISREG(s_.st_mode) and s_.st_size:
                    arrived.add((s_.st_size, int(round(s_.st_mtime)), real_ext(Path(n))))
        if arrived and gone:
            # a gone file that looks like one that arrived is likely moved: the next full run pairs the two and keeps what
            # was read from it (reuse_moved); until then search leaves it out of answers when it is not on disk (#170)
            qm = ",".join("?" * len(gone))
            look = {fid: (size, int(round(mt or 0)), ext) for fid, size, mt, ext in
                    con.execute(f"SELECT id, size, mtime, ext FROM files WHERE id IN ({qm})", gone)}
            wait = [fid for fid in gone if look.get(fid) in arrived]
            st_["waiting"] += len(wait)
            gone = [fid for fid in gone if fid not in set(wait)]
        if gone and len(gone) > len(rows) // 2:
            print(f"FINDR_GONE kept {len(gone)} of {len(rows)} files under {root} (too many at once to be deletions)", flush=True)
            st_["kept"] += len(gone)
            continue
        for fid in gone:
            drop(con, fid)
        st_["removed"] += len(gone)
    con.execute("INSERT OR REPLACE INTO meta VALUES('gone_at', ?)", (str(t0),))   # changes during this check are seen next time
    con.commit()
    print("FINDR_GONE " + json.dumps({**st_, "ms": int((time.time() - t_run) * 1000)}), flush=True)
    return st_


def reuse_moved(con, fresh, lost):
    """a file that left one place and turned up in another in the same run (moved, renamed, Downloads -> Documents) keeps
    what was read from it: its row takes the new path and name, so its text (extraction, OCR and conversion are the slow
    part), summary, photo vector and history stay. Same size, same modified time and same format on both sides, and only
    one file on each side with them (two would be copies: which is which is not known). Its meaning vectors are made
    again, since they carry the name and folders. Returns the ids kept"""
    k_ = lambda size, mtime, ext: (size, int(round(mtime or 0)), ext)
    olds, news = {}, {}
    for s, (size, mtime, fid, tr) in lost:
        if size:
            e = (con.execute("SELECT ext FROM files WHERE id=?", (fid,)).fetchone() or [None])[0]
            olds.setdefault(k_(size, mtime, e), []).append((fid, tr))
    for f in fresh:
        news.setdefault(k_(f[2], f[3], f[4]), []).append(f)
    kept = set()
    for key_, o in olds.items():
        n = news.get(key_) or []
        if len(o) != 1 or len(n) != 1:
            continue
        (ofid, otr), (nfid, s, _, _, _, ntr, root) = o[0], n[0]
        if otr != ntr:
            continue   # a different tier now (a library folder, online-only): read as the new file it is
        drop(con, nfid)   # the row the walk just added for it
        name, rel = nfc(Path(s).name), nfc(s[len(root.as_posix()):])
        con.execute("UPDATE files SET path=?, name=? WHERE id=?", (s, name, ofid))
        for cid, text in con.execute("SELECT id, text FROM chunks WHERE file=? AND n>=0", (ofid,)).fetchall():
            con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, name, rel, nfc(text or "")))
        con.execute("UPDATE chunks SET emb=NULL WHERE file=? AND n>=0", (ofid,))
        try:
            con.execute("UPDATE chunks SET emb2=NULL WHERE file=? AND n>=0", (ofid,))
        except sqlite3.OperationalError:
            pass   # an index without the second lane's column
        kept.add(ofid)
    return kept


# every other table that keeps rows by file id. A file id is an INTEGER PRIMARY KEY, which SQLite hands out again after the
# highest one is deleted: a row left here would then describe the next new file (its summary, author, history)
FILE_TABLES = ("photo_emb", "cards", "docmeta", "events", "file_uid", "file_terms")   # file_uid: after relations.tombstone keeps its id (#203)


def drop(con, fid):
    try:
        relations.tombstone(con, fid)   # the file's stable id waits for it to turn up again (#203)
    except sqlite3.Error:
        pass
    con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=?)", (fid,))
    con.execute("DELETE FROM chunks WHERE file=?", (fid,)); con.execute("DELETE FROM files WHERE id=?", (fid,))
    for t in FILE_TABLES:
        try:
            con.execute(f"DELETE FROM {t} WHERE file=?", (fid,))
        except sqlite3.OperationalError:   # a table this index does not have (photos off, no traces collected)
            pass


class EngineEncoder:
    """document vectors from the search engine already running on this PC (POST /embed): the model stays in one process.
    The indexer's own copy (torch and the model) was 1.7 of its 1.75 GB, and next to the engine's 2.7 GB it went past what
    an 8 GB laptop leaves: the OS killed the engine (3 GB reproduction). Used when the engine holds the same encoder;
    FINDR_EMBED_VIA=local turns it off, and any failure falls back to the indexer's own copy."""
    STEP = 32

    def __init__(self, url, pw, name):
        self.url, self.name = url, name
        import base64
        self.hdr = {"Authorization": "Basic " + base64.b64encode(f"index:{pw}".encode()).decode(), "X-Findr": "1",
                    "Content-Type": "application/json"}

    @classmethod
    def connect(cls, name, wait=None):
        """the engine, or None. An engine still starting (engine_starting) is waited for, up to embed_wait() seconds: the
        indexer that gave up at once loaded its own copy of the model next to the engine's (0.7-1.1 GB, #198)"""
        if os.environ.get("FINDR_EMBED_VIA", "auto") == "local" or not name:
            return None
        import urllib.error
        d = os.environ.get("FINDR_DATA")
        port = int(os.environ.get("FINDR_APP_PORT") or 8096)
        wait, explicit = (embed_wait() if wait is None else wait), bool(os.environ.get("FINDR_EMBED_WAIT")) or wait is not None
        t0, said = time.time(), False
        while True:
            try:
                pw = Path(d, ".password").read_text().strip() if d else ""
            except OSError:
                pw = None   # a first start makes the password file
            if pw is not None:
                e = cls(f"http://127.0.0.1:{port}", pw, name)
                try:
                    e.vectors(["준비"])
                    if said:
                        print(f"FINDR_EMBED_WAIT the engine is up after {time.time() - t0:.0f}s", flush=True)
                    return e
                except urllib.error.HTTPError as x:   # it answered: another encoder (409), an old engine without /embed
                    if x.code != 503:
                        return None
                except Exception:   # noqa: BLE001  (no engine yet)
                    pass
            if time.time() - t0 >= wait or not (explicit or engine_starting(port)):
                return None
            if not said:
                said = True
                print(f"FINDR_EMBED_WAIT the engine is starting: waiting up to {wait:.0f}s instead of loading a second copy", flush=True)
            time.sleep(EMBED_WAIT_STEP)

    def vectors(self, texts):
        import base64
        import urllib.request
        out = []
        # this run's share of the cores (FINDR_THREADS): the engine computes the batch with no more threads than that
        threads = int(os.environ.get("FINDR_THREADS") or 0)
        for i in range(0, len(texts), self.STEP):
            req = urllib.request.Request(self.url + "/embed", json.dumps({"encoder": self.name, "texts": texts[i:i + self.STEP],
                                                                         **({"threads": threads} if threads else {})}).encode(),
                                         self.hdr)
            r = json.loads(urllib.request.urlopen(req, timeout=300).read())
            out.append(np.frombuffer(base64.b64decode(r["vecs"]), np.float16).reshape(-1, r["dim"]))
        return np.concatenate(out) if out else np.zeros((0, 1), np.float16)


EMBED_WAIT_START = 180   # seconds a starting engine is waited for: a forced restart came back in 16-24 s (#198); a slow PC's
# first start loads torch and the models for a minute or more
EMBED_WAIT_STEP = 2
STARTING_FILE = "engine_starting.json"   # written by `findr serve` at its start, removed once its models are loaded


def embed_wait():
    """seconds to wait for the engine: FINDR_EMBED_WAIT (0: never), else EMBED_WAIT_START, used only while an engine says it
    is starting"""
    try:
        return max(0.0, float(os.environ.get("FINDR_EMBED_WAIT") or EMBED_WAIT_START))
    except ValueError:
        return float(EMBED_WAIT_START)


def engine_starting(port=None, now=None):
    """an engine of this data folder is starting: its mark is there, recent, for this port, and its process is alive"""
    d = os.environ.get("FINDR_DATA")
    if not d:
        return False
    try:
        m = json.loads(Path(d, STARTING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    now = time.time() if now is None else now
    if not isinstance(m, dict) or now - float(m.get("t") or 0) > max(EMBED_WAIT_START, embed_wait()) + 60:
        return False   # an engine that died while starting left its mark behind
    if port is not None and m.get("port") and int(m["port"]) != int(port):
        return False
    try:
        import psutil
        return psutil.pid_exists(int(m.get("pid") or 0))
    except ImportError:
        return True
    except (ValueError, TypeError):
        return False


def _meta_encoder(con):
    r = con.execute("SELECT value FROM meta WHERE key='encoder'").fetchone()
    return r[0] if r else None


LOCAL_STEP = 32   # texts per forward pass of the indexer's own encoder


def embed_input(text, path):
    """what the encoder reads for one piece: the file's name and folders, then the piece"""
    return f"{label(Path(path))}\n{text}"[:1200]


def _input_key(text, path):
    import hashlib
    return hashlib.blake2b(embed_input(text, path).encode("utf-8", "surrogatepass"), digest_size=16).digest()


def _inputs(con, cids):
    """the encoder inputs of these pieces, in this order (read now, a batch at a time)"""
    got = {cid: embed_input(t, p) for cid, t, p in con.execute(
        f"SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file WHERE c.id IN ({','.join(str(int(c)) for c in cids)})")}
    return [got[c] for c in cids]


def embed(con, model, names_only=False):
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if model in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (model,)); con.commit()   # a new index records its encoder
    cap = int(os.environ.get("FINDR_FIRST_PIECES") or 0)   # first pass on a large PC: each file's opening pieces only
    if names_only:   # every file's name and folders, content or not (a file read later gets new pieces and vectors)
        sel = """SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                 WHERE c.emb IS NULL AND f.tier>0 AND c.n=0 AND COALESCE(c.text,'')='' ORDER BY f.mtime DESC"""
    else:
        sel = f"""SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                  WHERE c.emb IS NULL AND f.tier>0 AND COALESCE(f.pending,0) IN (0,2) {"AND c.n < %d" % cap if cap > 0 else ""}
                  ORDER BY (c.n > 1), f.mtime DESC, c.n"""   # every file's opening pieces first, newest first
    # identical inputs (a copied file, repeated boilerplate under the same name) are computed once. The pieces are looked at
    # as they come and only a fingerprint of each input is kept; the texts are read again a batch at a time below. A deep run
    # held every waiting piece's text twice (the rows, then the inputs built from them): gigabytes on a PC of long documents
    groups, n_rows = {}, 0
    for cid, t, p in con.execute(sel):
        groups.setdefault(_input_key(t, p), []).append(cid); n_rows += 1
    later = deep_left(con, cap)[0] if cap > 0 and not names_only else 0   # with the unread rest of long files (#67-1)
    if not n_rows:   # nothing to embed: no encoder load (three loads cost ~7 s per run with nothing new)
        con.commit(); print(f"FINDR_EMBED 0 chunks 0s (computed 0, identical skipped 0" + (f", later pieces left for a deep run {later})" if later else ")"))
        return
    name = _meta_encoder(con)
    eng = EngineEncoder.connect(name) if name in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") else None
    enc = None

    def local():
        """the indexer's own copy of the encoder (no engine, or the engine went away)"""
        from enc_eg2 import EG2, make_encoder
        import torch
        e = make_encoder(name) if name in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") else None
        if e is None:
            raise RuntimeError(f"no encoder for {name}")
        if not isinstance(e, EG2):
            e.tok.model_max_length = 512
        if os.environ.get("FINDR_THREADS"):   # background indexing: leave CPU cores for the person's own work
            torch.set_num_threads(max(1, int(os.environ["FINDR_THREADS"])))
        return e

    def vecs(texts):
        nonlocal eng, enc
        if eng is not None:
            try:
                return eng.vectors(texts)
            except Exception as e:   # noqa: BLE001  (the engine stopped or was restarted): one that comes back is waited
                eng = EngineEncoder.connect(name)   # for (engine_starting); else carry on with our own copy
                if eng is not None:
                    try:
                        return eng.vectors(texts)
                    except Exception:   # noqa: BLE001
                        eng = None
                print(f"FINDR_EMBED engine gone ({type(e).__name__}): this run computes its own vectors", flush=True)
        if enc is None:
            enc = local()
        import torch
        out = []
        with torch.no_grad():
            # 32 texts at a time, as the engine computes them: 256 texts of 512 tokens at once held gigabytes of attention
            # scores on a CPU for one batch, the same vectors either way
            for k in range(0, len(texts), LOCAL_STEP):
                out.append(emb512(enc, texts[k:k + LOCAL_STEP]).cpu().numpy().astype(np.float16))
        return np.concatenate(out) if out else np.zeros((0, 1), np.float16)

    def let_go():
        nonlocal enc
        held, enc = enc is not None, None   # vecs() loads it again when the next batch needs it
        return held

    if eng is None:
        enc = local()
    _RELEASE.append(let_go)
    try:
        _embed_batches(con, groups, n_rows, later, name, names_only, eng, vecs)
    finally:
        # the model goes with the stage: the names stage's copy stayed through reading every file's content (0.9 GB on an
        # 8 GB laptop, #198); the next stage loads it again only when it has pieces to embed
        if let_go in _RELEASE:
            _RELEASE.remove(let_go)
        if let_go():
            from governor import give_back
            give_back()


def _embed_batches(con, groups, n_rows, later, name, names_only, eng, vecs):
    print(f"FINDR_EMBED_VIA {'engine' if eng is not None else 'local'} ({name})", flush=True)
    uniq = list(groups.values()); groups.clear()   # [chunk ids with one input], in the order above
    t0 = last = time.time(); warned = False
    stage = "namevec" if names_only else "meaning"
    progress(stage, 0, len(uniq))   # the stage shows at once, not after its first batch
    i, bs = 0, 256
    while i < len(uniq):
        pause_wait()
        b = uniq[i:i + bs]
        tb = time.time()
        v = vecs(_inputs(con, [cids[0] for cids in b]))
        if time.time() - tb > 8 and bs > 32:
            bs //= 2   # a slow PC (a large model on a CPU: 50 s per 256) reports more often
        con.executemany("UPDATE chunks SET emb=?, emb2=NULL WHERE id=?", [(x.tobytes(), cid) for x, cids in zip(v, b) for cid in cids])   # changed text: its side vector is stale
        done_ = i + len(b); i = done_
        rate = done_ / max(time.time() - t0, 1e-6); left = (len(uniq) - done_) / max(rate, 1e-6)
        tick(stage, done_, len(uniq), minutes_left=round(left / 60, 1))
        if done_ % 2560 < len(b) or time.time() - last > 30 or done_ == len(uniq):   # saved often
            con.commit(); last = time.time()
            print(f"  embedded {done_}/{len(uniq)} {last - t0:.0f}s | {rate:.0f} pieces/s | about {left / 60:.0f} min left", flush=True)
            if not warned and done_ >= 1024 and left > 6 * 3600 and name == "embeddinggemma-2":
                warned = True
                print("FINDR_SLOW 이 PC에서는 큰 모델이 느립니다. 작은 모델로 바꾸면 훨씬 빨라집니다:  findr setup --lite", flush=True)
    con.commit(); print(f"FINDR_EMBED {n_rows} chunks {time.time() - t0:.0f}s (computed {len(uniq)}, identical skipped {n_rows - len(uniq)}"
                        + (f", later pieces left for a deep run {later})" if later else ")"))


def user_idle_seconds():
    """seconds since the person last used the keyboard or mouse; None when this PC cannot tell"""
    try:
        if os.name == "nt":
            import ctypes

            class LII(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            li = LII(); li.cbSize = ctypes.sizeof(LII)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
                return None
            return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"HIDIdleTime" = (\d+)', out)
            return int(m.group(1)) / 1e9 if m else None
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) / 1000 if out.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


LOOK_EVERY = 5   # seconds between looks at the keyboard and mouse during an idle-time run


def person_is_back(within):
    """the app's idle-time runs give the PC back: true when the keyboard or mouse was used in the last `within` seconds"""
    if not within:
        return False
    idle = user_idle_seconds()
    return idle is not None and idle < within


# office documents: their first pieces get the larger model's vector before code, logs and data files
UPGRADE_DOCS = ("pdf", "hwp", "hwpx", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "key", "pages", "numbers", "md", "txt")
FIRST_DONE = 6   # upgrade(first_only=True): every first piece has its side vector (0-2 are the full upgrade's, 3-5 the CLI's)


def upgrade(con, target, minutes=50, bs=None, first_only=False):
    """compute `target` vectors next to the current ones (column emb2), at most `minutes` per run; when all are there,
    switch in one transaction (emb <- emb2, meta encoder <- target). Returns 0 switched / 2 more runs needed / 1 nothing to do.
    Each file's first piece (n=0) comes first, office documents before the rest, newest first: the search engine can use
    those alone as a second meaning lane (LANE2 in search.py) long before the switch. On 4,771 real files the answer was
    among the first 10 candidates for 33 of 45 questions with that lane, 22 without (2,000 virtual questions: 1,850, 1,530).
    first_only: stop there, without switching, for a PC where the whole upgrade is too slow; returns FIRST_DONE (6) when
    every first piece is there, 2 when more runs are needed. Its progress goes to lane2.json: the hours left of a
    first-pieces run must not be read as the whole upgrade's (the CLI turns the upgrade off on that number)."""
    from enc_eg2 import encoder_of, make_encoder
    import torch
    cur = encoder_of(con)
    if cur == target:
        print(f"FINDR_UPGRADE done already ({target})", flush=True); return 1
    if "emb2" not in [r[1] for r in con.execute("PRAGMA table_info(chunks)")]:
        con.execute("ALTER TABLE chunks ADD COLUMN emb2 BLOB")
    mark = con.execute("SELECT value FROM meta WHERE key='upgrade'").fetchone()
    if not mark or mark[0] != target:   # a new target (or none yet): side vectors of another encoder are not reused
        con.execute("UPDATE chunks SET emb2=NULL"); con.execute("INSERT OR REPLACE INTO meta VALUES('upgrade', ?)", (target,))
    con.commit()
    if os.environ.get("FINDR_THREADS"):
        torch.set_num_threads(max(1, int(os.environ["FINDR_THREADS"])))
    enc = make_encoder(target)
    if bs:
        enc.bs = bs
    elif getattr(enc, "dev", "cpu") == "cpu":
        enc.bs = int(os.environ.get("FINDR_BATCH") or 8)   # small batches on the CPU: memory stays low, each run stops on time
    t0 = time.time(); stop = t0 + minutes * 60; did = 0
    scope = " AND n=0" if first_only else ""   # what this run counts and fills
    pfile = "lane2.json" if first_only else "upgrade.json"
    docs = ",".join(f"'.{e}'" for e in UPGRADE_DOCS)
    total = con.execute(f"SELECT count(*) FROM chunks WHERE emb IS NOT NULL{scope}").fetchone()[0]
    # the app's idle-time run stops when the person comes back (FINDR_STOP_ON_INPUT = seconds of recent input that count):
    # on an 8 GB laptop a 10-minute run kept 2.7 GB and half the cores while the person searched. Smaller steps so it notices soon.
    back = float(os.environ.get("FINDR_STOP_ON_INPUT") or 0)
    step = 16 if back else 64
    last = {k: v for k, v in _last_upgrade().items() if k in ("rate", "hours_left")}   # read now: progress below rewrites the file
    looked = 0.0
    battery = os.environ.get("FINDR_STOP_ON_BATTERY") == "1"
    # first pieces first (documents, then the rest, newest first); after them the order used before (cards and second
    # pieces, then the deeper ones, newest file first). The order is taken once per run, ids only: asked again for each
    # step of 16 pieces, the sort went over every piece of the index each time
    work = [r[0] for r in con.execute(f"""SELECT c.id FROM chunks c JOIN files f ON f.id=c.file
                               WHERE c.emb IS NOT NULL AND c.emb2 IS NULL{' AND c.n=0' if first_only else ''}
                               ORDER BY (c.n != 0), (c.n = 0 AND lower(coalesce(f.ext, '')) NOT IN ({docs})), (c.n > 1), f.mtime DESC, c.n""")]
    pos = 0
    while True:
        rows = []
        while not rows and pos < len(work):
            ids_ = work[pos:pos + step]; pos += len(ids_)
            got = {r[0]: r for r in con.execute(f"""SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                       WHERE c.id IN ({','.join(str(int(i)) for i in ids_)}) AND c.emb IS NOT NULL AND c.emb2 IS NULL""")}
            rows = [got[i] for i in ids_ if i in got]
        if not rows:
            break
        returned = False
        if (back or battery) and time.time() - looked >= LOOK_EVERY:   # not every step: on a fast GPU 16 pieces take a fraction of a second
            looked = time.time(); returned = person_is_back(back)
            if not returned and battery:
                try:
                    from governor import on_battery
                    returned = on_battery()   # the charger came out: the rest waits for it
                except ImportError:
                    pass
        if not returned:
            free = disk_free_gb(_GB["db"])
            returned = free is not None and free < DISK_MIN_GB   # the disk is nearly full: stop, keep what is there
        if time.time() >= stop or returned:   # >=: minutes=0 stops at once on Windows' 15 ms clock too
            left = con.execute(f"SELECT count(*) FROM chunks WHERE emb IS NOT NULL AND emb2 IS NULL{scope}").fetchone()[0]
            rate = did / max(time.time() - t0, 1e-6)
            if returned:   # a run cut short (often while the model was still loading) says nothing about the speed: keep the
                # last run's estimate. Measured here, 0 pieces would read as 'thousands of hours' and turn the upgrade off for good
                print(f"FINDR_UPGRADE paused {total - left}/{total} | the person is back after {did} pieces", flush=True)
                progress_upgrade(target, total - left, total, file=pfile, back=True, **last); return 2
            print(f"FINDR_UPGRADE paused {total - left}/{total} | {rate:.1f} pieces/s | about {left / max(rate, 1e-6) / 3600:.1f} h of idle time left", flush=True)
            progress_upgrade(target, total - left, total, file=pfile, rate=round(rate, 2), hours_left=round(left / max(rate, 1e-6) / 3600, 1)); return 2
        with torch.no_grad():
            v = emb512(enc, [f"{label(Path(p))}\n{t}"[:1200] for _, t, p in rows]).cpu().numpy().astype(np.float16)
        con.executemany("UPDATE chunks SET emb2=? WHERE id=?", [(x.tobytes(), cid) for x, (cid, _, _) in zip(v, rows)])
        con.commit(); did += len(rows)
        if did % 640 < step:   # done: every piece but the ones still in this run's list (no count over the index each time)
            progress_upgrade(target, total - (len(work) - pos), total, file=pfile, **last)
    if first_only:   # the first pieces are there: the small model keeps answering, the second lane reads these
        progress_upgrade(target, total, total, file=pfile, first_only=True)
        print(f"FINDR_UPGRADE first pieces ready ({target}): {total} files, {did} this run in {time.time() - t0:.0f}s", flush=True)
        return FIRST_DONE
    # the switch rewrites every vector in one transaction: the write-ahead file grows to about the size of the index
    need = switch_room_gb(con)
    free = disk_free_gb(_GB["db"])
    if need and free is not None and free < need + DISK_MIN_GB:
        print(f"FINDR_UPGRADE waits for disk space: the switch needs about {need:.1f} GB, {free:.1f} GB free", flush=True)
        return 2
    with con:   # every vector has its counterpart: switch at once (search reloads when the recorded encoder changes)
        con.execute("UPDATE chunks SET emb=emb2 WHERE emb IS NOT NULL")
        con.execute("UPDATE chunks SET emb2=NULL")
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (target,))
        con.execute("DELETE FROM meta WHERE key='upgrade'")
    wal_trim(con)
    progress_upgrade(target, total, total, switched=True)
    print(f"FINDR_UPGRADE switched to {target}: {total} vectors in {time.time() - t0:.0f}s this run", flush=True)
    return 0


def _last_upgrade():
    d = os.environ.get("FINDR_DATA")
    try:
        return json.loads(Path(d, "upgrade.json").read_text(encoding="utf-8")) if d else {}
    except (OSError, ValueError):
        return {}


def progress_upgrade(target, done, total, file="upgrade.json", **kw):
    """the upgrade's own progress file (the indexing banner stays for indexing); a first-pieces run writes lane2.json"""
    d = os.environ.get("FINDR_DATA")
    if d:
        try:
            Path(d, file).write_text(json.dumps({"target": target, "done": done, "total": total, "t": time.time(), **kw}), encoding="utf-8")
        except OSError:
            pass


def emb512(enc, texts):
    if hasattr(enc, "st"):   # EmbeddingGemma 2: document prompt, 512 tokens
        return enc.emb(texts, query=False)
    import torch
    import torch.nn.functional as F
    b = enc.tok([enc.pre + t for t in texts], padding=True, truncation=True, max_length=512, return_tensors="pt").to(enc.dev)
    with torch.autocast(enc.dev, dtype=torch.bfloat16, enabled=enc.dev == "cuda"):
        h = enc.m(**b).last_hidden_state
    if enc.pool == "cls":
        v = h[:, 0]
    else:
        mk = b["attention_mask"].unsqueeze(-1).to(h.dtype); v = (h * mk).sum(1) / mk.sum(1)
    return F.normalize(v.float(), dim=-1)


if __name__ == "__main__":
    sys.exit(main() or 0)
