"""Episodic cues: how a person remembers handling a file (who sent it, where it came from, what they did with it, what they were
doing then) checked against the index's memory traces (docmeta, events, calendar). No language model.
    cues(q)            -> list of cue dicts parsed from the question
    files_for(con, c)  -> set of file ids that satisfy one cue (None when the index has no traces for it)
Round 2: people are called by surname+title while records hold full names; groups (재무팀, 전원) receive files; a channel word
next to a sending verb (메일로 보낸, 단톡방에 올린) is how it was sent, not where it came from; "~한테 받은" is receiving."""
import datetime as dt
import re

SURN = "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반라왕금옥육인맹제모탁국어은편용예경봉"
TITLE = r"(?:부장|과장|대리|팀장|차장|실장|이사|대표|원장|교수|선생님|주임|사원|매니저|소장|국장|센터장|본부장|상무|전무)"
NOT_NAME = {"주에", "전날", "다음", "이번", "지난", "저번", "오늘", "어제", "내일", "월말", "초에", "말에", "전에", "후에", "직전", "회의", "미팅"}
P_TITLED = re.compile(rf"(?<![가-힣])([{SURN}][가-힣]{{0,2}})?\s*({TITLE})(?:님)?")
P_BARE = re.compile(rf"(?<![가-힣])([{SURN}][가-힣]{{2}})(?=(?:(?:님|씨)(?:이가|이랑|이|가|께서|에게|한테|의|와|과|랑)?|이가|이랑|이|가|께서|에게|한테|의|와|과|랑)(?![가-힣]))")   # a name is followed by 님/씨 or a particle
P_GROUP = re.compile(r"(?<![가-힣])([가-힣]{1,6}팀|전사|전원|전\s*직원|전체)(?=\s*(?:에게|한테|께|에|으로|로|메일)?)")
SENDV = r"(?:보낸|보냈던|제출한|전달한|회신한|공유한|송부한|넘긴|(?<=방에 )올린|(?<=판에 )올린|(?<=톡에 )올린|돌린|보내드린)"
RECVV = r"(?:받은|받았던|온|보내온|보내\s*준|보내준|준|넘겨\s*준|넘겨준|건네준)"
SENT_TO = re.compile(rf"^\s*(?:님)?(?:에게|한테|께|에|으로|로)?\s*(?:[가-힣]{{0,4}}\s*)?(?:메일로\s*|단톡방에\s*|메신저로\s*|카톡으로\s*)?{SENDV}")
RECV_FROM = re.compile(rf"^\s*(?:님)?(?:(?:이|가|께서)\s*.{{0,10}}?(?:{SENDV}|{RECVV})|(?:한테|에게서|한테서|로부터|에게|께)\s*.{{0,8}}?{RECVV})")
SEND_ANY = re.compile(rf"(?<![가-힣]){SENDV}(?!\s*(?:사람|분))")
RECV_ANY = re.compile(rf"(?<![가-힣])(?:받은|받았던|보내온|보내준|보내\s*준|다운받|다운로드|내려받)")
SRC = [("saved_from_mail", re.compile(r"메일|이메일|e-?mail|첨부", re.I)), ("saved_from_messenger", re.compile(r"카톡|카카오톡|메신저|단톡|슬랙|DM|문자로", re.I)),
       ("downloaded", re.compile(r"다운로드|다운받|다운 받|내려받|포털에서|사이트에서"))]
ACT = [("printed", re.compile(r"인쇄|출력|프린트|뽑아")), ("renamed", re.compile(r"이름\s*(?:바꾼|바꿨|변경)")), ("copied", re.compile(r"복사해|사본\s*만든"))]
WHEN = re.compile(r"(회의|미팅|면담|면접|발표|세미나|교육|워크숍|간담회|설명회|보고회|통화|점검|방문)\s*(?:을|를)?\s*"
                  r"(전날|직전|전에|전|앞두고|하기\s*전|가기\s*전|준비|후에|후|끝나고|하고\s*나서|마치고|다녀와서|다음\s*날|중에|하면서)")
STOP = {"회의", "미팅", "자료", "파일", "문서", "거", "것", "그", "전", "후", "직전", "때", "관련", "버전", "최종", "수정", "전날"}


import os
TODAY = dt.datetime.fromisoformat(os.environ["FINDR_TODAY"]) if os.environ.get("FINDR_TODAY") else None   # evaluation sets fix 'today'
VERB_TYPES = [(re.compile(r"고친|수정한|손본|고쳐"), {"modified"}), (re.compile(r"만든|작성한|처음\s*쓴|만들어"), {"created"}),
              (re.compile(r"열어\s*본|열어봤|열어\s*봤|읽은"), {"opened"}), (re.compile(r"인쇄|뽑은|뽑아|출력"), {"printed"}),              (re.compile(SENDV_TXT := r"보낸|보냈|제출한|돌린|전달한|회신한|송부한"), {"sent"}),
              (re.compile(r"받은|받았|보내온|다운|내려받|저장한"), {"saved_from_mail", "saved_from_messenger", "downloaded"})]
def verb_types(q):
    """which kind of action the person remembers doing ('끝나고 인쇄한' = printed), as event types; empty = not said"""
    out = set()
    for rx, ts in VERB_TYPES:
        if rx.search(q):
            out |= ts
    return out


WEEKN = {"첫째": 0, "첫": 0, "둘째": 1, "셋째": 2, "넷째": 3, "다섯째": 4}


def _month_range(y, m, part=None):
    a = dt.datetime(y, m, 1); b = dt.datetime(y + (m == 12), m % 12 + 1, 1)
    if part == "초":
        return a, a + dt.timedelta(days=11)
    if part == "중순":
        return a + dt.timedelta(days=9), a + dt.timedelta(days=21)
    if part in ("말", "말경"):
        return b - dt.timedelta(days=11), b
    return a, b


YEARW = {"재작년": -2, "지지난해": -2, "작년": -1, "지난해": -1, "올해": 0, "금년": 0}
SEASON = {"봄": (3, 3), "여름": (6, 3), "가을": (9, 3), "겨울": (12, 3)}   # start month, length


def action_dates(q, now=None):
    """the date range of an action in the question ('8월 19일에', '9월 초에', '9월 셋째 주에', '지난주에', '지난달',
    '작년 8월', '작년 여름', '겨울방학', '재작년에') or None"""
    now = now or TODAY or dt.datetime.now()
    r = _year_season(q, now)
    if r:
        return r
    yw = re.search(r"(재작년|지지난해|작년|지난해|올해|금년)\s*(?=\d{1,2}\s*월)", q)
    r = _action_dates(q, now, now.year + YEARW[yw.group(1)] if yw else None)
    if r and yw:
        return r[0], r[1], yw.group(1) + " " + r[2]
    return r


def _year_season(q, now):
    """'작년 여름', '올해 봄에', '겨울방학', '여름휴가', '작년에' (a whole year)"""
    m = re.search(r"(재작년|지지난해|작년|지난해|올해|금년)?\s*(봄|여름|가을|겨울)(?:\s*(?:에|철|휴가|방학|쯤|무렵|때))", q) or \
        re.search(r"(재작년|지지난해|작년|지난해|올해|금년)\s*(봄|여름|가을|겨울)(?![가-힣])", q)
    if m:
        m0, n = SEASON[m.group(2)]
        y = now.year + (YEARW[m.group(1)] if m.group(1) else 0)
        a = dt.datetime(y, m0, 1)
        if a > now:   # not begun yet: a bare season or '올해 겨울' means the latest one that has begun
            a = a.replace(year=a.year - 1)
        b = dt.datetime(a.year + (a.month + n - 1) // 12, (a.month + n - 1) % 12 + 1, 1)   # winter: Dec to the end of Feb
        return a, b, m.group(0).strip()
    m = re.search(r"(재작년|지지난해|작년|지난해)(?:에|\s*이맘때|\s*쯤)?(?!\s*\d{1,2}\s*월)(?![가-힣])", q)
    if m:
        y = now.year + YEARW[m.group(1)]
        if "이맘때" in m.group(0):
            c = now.replace(year=y); return c - dt.timedelta(days=30), c + dt.timedelta(days=30), m.group(0)
        return dt.datetime(y, 1, 1), dt.datetime(y + 1, 1, 1), m.group(0)
    return None


def _action_dates(q, now, year=None):
    y = year if year is not None else now.year
    back = lambda d: d if (d <= now or year is not None) else d.replace(year=d.year - 1)
    # a span: '10월 20일부터 25일 사이', '9월 3일~9월 10일', '10월 1일에서 5일까지'
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:부터|에서|~|-|∼)\s*(?:(\d{1,2})\s*월\s*)?(\d{1,2})\s*일", q)
    if m:
        a = back(dt.datetime(y, int(m.group(1)), int(m.group(2))))
        b = dt.datetime(a.year, int(m.group(3) or m.group(1)), int(m.group(4)))
        b = b if b >= a else b.replace(year=b.year + 1)
        return a, b + dt.timedelta(days=1), m.group(0)
    m = re.search(r"(\d{1,2})\s*월\s*(?:부터|에서|~|-|∼)\s*(\d{1,2})\s*월\s*(?:까지|사이)", q)
    if m:
        a = _month_range(y, int(m.group(1)))[0]; b = _month_range(y, int(m.group(2)))[1]
        if a > now:
            a = a.replace(year=a.year - 1)
        if b < a:
            b = b.replace(year=b.year + 1)
        return a, b, m.group(0)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.search(r"(\d{1,2})\s*월\s*(?:넘어서|넘어|이후|들어서|들어|부터)(?!\s*\d)", q)
    if m:   # '9월 넘어서', '11월 들어서': from that month on
        a = _month_range(y, int(m.group(1)))[0]
        a = a if a <= now else a.replace(year=y - 1)
        return a, day0 + dt.timedelta(days=1), m.group(0)
    m = re.search(r"(지난\s*주|저번\s*주|이번\s*주|금주)?\s*([월화수목금토일])요일", q)
    if m:   # '지난주 금요일', '이번 주 월요일', or a bare '화요일에' = the most recent such day
        wd = "월화수목금토일".index(m.group(2)); monday = day0 - dt.timedelta(days=now.weekday())
        if m.group(1) and re.match(r"지난|저번", m.group(1)):
            d = monday - dt.timedelta(days=7) + dt.timedelta(days=wd)
        elif m.group(1):
            d = monday + dt.timedelta(days=wd)
        else:
            d = day0 - dt.timedelta(days=(now.weekday() - wd) % 7)
        return d, d + dt.timedelta(days=1), m.group(0).strip()
    for w, n in (("그저께", 2), ("그제", 2), ("어제", 1), ("오늘", 0)):
        if re.search(w + r"(?:에|\s|$|오전|오후|저녁|아침|밤)", q):
            return day0 - dt.timedelta(days=n), day0 - dt.timedelta(days=n - 1), w
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", q)
    if m:
        d = dt.datetime(y, int(m.group(1)), int(m.group(2))); d = d if (d <= now or year is not None) else d.replace(year=y - 1)
        return d, d + dt.timedelta(days=1), m.group(0)
    m = re.search(r"(\d{1,2})\s*월\s*(첫째|첫|둘째|셋째|넷째|다섯째|마지막)\s*주(?:에|쯤|경)?", q)
    if m:
        a, b = _month_range(y, int(m.group(1)))
        if m.group(2) == "마지막":
            return b - dt.timedelta(days=8), b, m.group(0)
        s = a + dt.timedelta(days=7 * WEEKN[m.group(2)] - 2)
        return s, s + dt.timedelta(days=10), m.group(0)
    m = re.search(r"(\d{1,2})\s*월\s*(초|중순|말경|말)?\s*(?:에|쯤|경|께|무렵)(?![가-힣])|(\d{1,2})\s*월\s*(초|중순|말경|말)(?![가-힣])", q) or \
        (re.search(r"(\d{1,2})\s*월()()()(?!\s*\d)", q) if year is not None else None)
    if m:   # '8월 월간실적' is what the document is about, not when it was handled: only '8월에/8월 초/8월쯤'
        mon = int(m.group(1) or m.group(3)); part = m.group(2) or m.group(4)
        a, b = _month_range(y, mon, part)
        return a, b, m.group(0)
    if False:
        a, b = _month_range(y, int(m.group(1)), m.group(2))
        return a, b, m.group(0)
    wk = now - dt.timedelta(days=now.weekday())
    if re.search(r"지난\s*주|저번\s*주", q):
        return wk - dt.timedelta(days=7), wk, "지난주"
    if re.search(r"이번\s*주", q):
        return wk, now + dt.timedelta(days=1), "이번 주"
    if re.search(r"지난\s*달|저번\s*달", q):
        a, b = _month_range(y if now.month > 1 else y - 1, now.month - 1 or 12)
        return a, b, "지난달"
    return None


def _role(rest):
    if RECV_FROM.search(rest):
        return "from"
    if SENT_TO.search(rest):
        return "sent_to"
    return "any"


def cues(q):
    out, spans = [], []
    for m in P_TITLED.finditer(q):
        name = (m.group(1) or "").strip()
        if name in NOT_NAME or (name and name[-1] in "에의는을를"):
            name = ""
        out.append({"kind": "person", "name": name, "title": m.group(2), "role": _role(q[m.end():m.end() + 18]), "text": m.group(0).strip()})
        spans.append((m.start(), m.end()))
    for m in P_BARE.finditer(q):
        if any(a <= m.start() < b for a, b in spans) or m.group(1) in NOT_NAME:
            continue
        out.append({"kind": "person", "name": m.group(1), "title": None, "role": _role(q[m.end():m.end() + 18]), "text": m.group(1)})
        spans.append((m.start(), m.end()))
    for m in P_GROUP.finditer(q):
        if any(a <= m.start() < b for a, b in spans):
            continue
        rest = q[m.end():m.end() + 14]
        if SEND_ANY.search(rest) or re.match(r"\s*메일", rest):
            out.append({"kind": "group", "name": re.sub(r"\s+", "", m.group(1)), "role": "sent_to", "text": m.group(1)})
    sending = bool(SEND_ANY.search(q)) and not any(c["kind"] == "person" and c["role"] == "from" for c in out)
    receiving = bool(RECV_ANY.search(q)) or any(c["kind"] == "person" and c["role"] == "from" for c in out)
    if not sending:   # a channel next to a sending verb is how it was sent, not where it came from
        for t, rx in SRC:
            if rx.search(q) and (receiving or t == "downloaded"):
                out.append({"kind": "source", "type": t, "text": rx.search(q).group(0)})
    if sending:
        out.append({"kind": "action", "type": "sent", "text": SEND_ANY.search(q).group(0)})
    for t, rx in ACT:
        if rx.search(q):
            out.append({"kind": "action", "type": t, "text": rx.search(q).group(0)})
    rng = action_dates(q)
    if rng:   # the date belongs to the action the person remembers, not to the file's last change
        types = set()
        from_person = any(c["kind"] == "person" and c["role"] == "from" for c in out)
        for rx, ts in VERB_TYPES:
            if rx.search(q):
                types |= ({"saved_from_mail", "saved_from_messenger", "downloaded"} if from_person and ts == {"sent"} else ts)
        if types:
            out.append({"kind": "when", "range": (rng[0].isoformat(), rng[1].isoformat()), "types": sorted(types), "text": rng[2]})
    if re.search(r"마지막으로\s*(?:고친|손본|수정한|건드린)", q):
        for c in out:
            if c["kind"] == "person":
                c["last"] = True
    if re.search(r"여러\s*(?:번|차례)\s*(?:고친|고쳐진|수정한|수정된|손본)|계속\s*고친", q):
        out.append({"kind": "revised", "text": "여러 번"})
    m = WHEN.search(q)
    if m:
        w = m.group(2)
        side = "daybefore" if w == "전날" else "before" if re.search(r"직전|전|앞두고|준비", w) else "during" if re.search(r"중에|하면서", w) else "after"
        words = [x for x in re.findall(r"[가-힣A-Za-z0-9]{2,}", q[:m.start()]) if x not in STOP][-3:]
        out.append({"kind": "activity", "event": m.group(1), "side": side, "words": words, "text": m.group(0), "types": sorted(verb_types(q))})
    return out


FILLER = re.compile(r"(?<![가-힣])(?:그|그거|거|것|파일|문서|자료|좀|찾아\s*줘|찾아줘|어디|있던|했던|하던|만든|고친|열어\s*본|열어본|본|정리한|정리해\s*둔|"
                    r"보낸|보냈던|받은|받았던|온|준|올린|돌린|제출한|전달한|공유한|인쇄한|인쇄해서|출력한|뽑은|이|가|을|를|에|에게|한테|께|로|으로|의|와|과|에서|"
                    r"메일로|메일|카톡으로|카톡|단톡방에|메신저로|다운받은|전에|후에|직전에|끝나고|직후)(?![가-힣])")


def content_part(q, cs):
    """the question without its episodic phrases: what the file is ABOUT, for the content check"""
    t = q
    for c in cs:
        t = t.replace(c["text"], " ")
    t = FILLER.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _has(con, table):
    return con.execute("SELECT count(*) FROM sqlite_master WHERE name=?", (table,)).fetchone()[0] > 0


def _person_match(s, c):
    """'김 부장' matches '김 부장', '김태호 부장' and a bare full name '김태호'; '배성준' matches it literally; '팀장' matches '팀장'"""
    if not s:
        return False
    s = s.replace(" ", "")
    name, title = c["name"], c.get("title")
    if not name:
        return bool(title) and title in s
    if len(name) >= 2:
        return name in s
    if not s.startswith(name):
        return False
    if title is None or title in s:
        return True
    return not re.search(TITLE, s) and 2 <= len(s) <= 4   # a bare full name with the same surname


EVERYONE = {"전직원", "전원", "전체", "전사"}


def _group_match(s, name):
    s = s.replace(" ", "")
    return name in s or (name in EVERYONE and any(w in s for w in EVERYONE))


_CACHE = {}


def _traces(con):
    """events and docmeta read once per index and kept until the trace tables change (row counts as the signature)"""
    sig = (con.execute("SELECT count(*), max(id) FROM events").fetchone(),
           con.execute("SELECT count(*) FROM docmeta").fetchone()[0] if _has(con, "docmeta") else 0)
    key = id(con)
    if key not in _CACHE or _CACHE[key][0] != sig:
        ev = con.execute("SELECT file, type, time, person, source FROM events ORDER BY time").fetchall()
        dm = con.execute("SELECT file, author, last_by, revision FROM docmeta").fetchall() if _has(con, "docmeta") else []
        by_type, by_who, mods, last_mod = {}, {}, {}, {}
        for f, t, tm, p, src in ev:   # grouped once: by type (time-sorted), by person/source string, modification counts
            by_type.setdefault(t, ([], []))
            by_type[t][0].append(tm); by_type[t][1].append(f)
            for w in {p, src} - {None, ""}:
                by_who.setdefault(w, []).append((f, t))
            if t == "modified":
                mods[f] = mods.get(f, 0) + 1; last_mod[f] = p
        names = set(by_who) | {x for r in dm for x in r[1:3] if x}
        _CACHE[key] = (sig, by_type, by_who, mods, last_mod, dm, names)
    return _CACHE[key][1:]


def _in_range(by_type, types, a, b):
    import bisect
    out = set()
    for t in types:
        if t in by_type:
            times, files = by_type[t]
            out.update(files[bisect.bisect_left(times, a):bisect.bisect_left(times, b)])
    return out


_FS = {}


def _fs_in_range(con, a, b):
    """files whose last-saved time (file system) falls in [a, b): ISO strings, local time"""
    import bisect
    n = con.execute("SELECT count(*), max(mtime) FROM files").fetchone()
    if id(con) not in _FS or _FS[id(con)][0] != n:
        rows = sorted((dt.datetime.fromtimestamp(m).isoformat(), f) for f, m in con.execute("SELECT id, mtime FROM files WHERE mtime IS NOT NULL"))
        _FS[id(con)] = (n, [r[0] for r in rows], [r[1] for r in rows])
    _, times, files = _FS[id(con)]
    return set(files[bisect.bisect_left(times, a):bisect.bisect_left(times, b)])


EVENT_KINDS = {"source", "action", "when", "activity"}


def files_for(con, c):
    """files meeting one cue, or None when the index cannot know (no traces of that kind, or the person/group never appears):
    a missing record is not evidence that it did not happen"""
    if not _has(con, "events"):
        return None
    by_type, by_who, mods, last_mod, dm, names = _traces(con)
    n_events = sum(len(v[1]) for v in by_type.values())
    fs_types = {"modified"} | ({"created"} if n_events == 0 else set())   # no activity record: last-saved time stands in for 'made'
    if c["kind"] == "when" and os.environ.get("EPI_FS", "1") == "1" and set(c["types"]) & fs_types:
        # the file system keeps every file's last-saved time even when no activity record exists: '지난주에 고친/만든' can use it
        hit = _in_range(by_type, c["types"], c["range"][0], c["range"][1]) if n_events else set()
        if n_events == 0 and os.environ.get("EPI_FS_SOFT", "1") == "1":
            c["_soft"] = True   # only the file system's last-saved time: a hint that can lift files, never a reason to say 'not here'
        return hit | _fs_in_range(con, c["range"][0], c["range"][1])
    if c["kind"] in EVENT_KINDS and n_events == 0:
        return None
    if c["kind"] == "revised" and not mods and not any(r for *_, r in dm):
        return None
    if c["kind"] in ("person", "group") and c.get("role") in ("from", "sent_to") and n_events == 0 and not c.get("last"):
        return None   # who sent / who received is only in the activity record
    if c["kind"] == "person" and not c["name"]:   # a title alone ('대표') is only usable if records carry that title
        if not c.get("title") or not any(c["title"] in x for x in names):   # neither name nor title: nothing to look up
            return None
    if c["kind"] == "person" and c["name"] and not any(_person_match(x, c) for x in names):
        return None   # this person appears nowhere in the records: cannot tell
    if c["kind"] == "group" and not any(_group_match(x, c["name"]) for x in names):
        return None
    recv = {"saved_from_mail", "saved_from_messenger", "downloaded"}
    if c["kind"] == "when":
        return _in_range(by_type, c["types"], c["range"][0], c["range"][1])
    if c["kind"] == "revised":
        return {f for f, k in mods.items() if k >= 3} | {f for f, a, b, r in dm if (r or 0) >= 3}
    if c["kind"] == "person" and c.get("last"):   # '마지막으로 고친 사람' = last editor
        return {f for f, a, b, r in dm if _person_match(b, c)} | {f for f, p in last_mod.items() if _person_match(p, c)}
    if c["kind"] in ("person", "group"):
        hit = set()
        for w, rows in by_who.items():   # match each distinct name once, not every event
            ok = _group_match(w, c["name"]) if c["kind"] == "group" else _person_match(w, c)
            if not ok:
                continue
            for f, t in rows:
                if c["role"] == "from" and t not in recv:
                    continue
                if c["role"] == "sent_to" and t != "sent":
                    continue
                hit.add(f)
        if c["kind"] == "person" and c["role"] in ("from", "any"):
            hit |= {f for f, a, b, r in dm if _person_match(a, c) or _person_match(b, c)}
        return hit
    if c["kind"] in ("source", "action"):
        return set(by_type.get(c["type"], ([], []))[1])
    if c["kind"] == "activity":
        if not _has(con, "calendar"):
            return None
        cal = con.execute("SELECT title, start, end, attendees FROM calendar").fetchall()
        meets = [(dt.datetime.fromisoformat(s), dt.datetime.fromisoformat(e or s)) for t, s, e, att in cal
                 if c["event"] in t or any(w in t or w in (att or "") for w in c["words"])]
        if not meets:
            return set()
        span = {"before": (dt.timedelta(hours=4), dt.timedelta(0)), "daybefore": (dt.timedelta(hours=40), dt.timedelta(hours=0))}
        win = []
        for s, e in meets:
            if c["side"] in span:
                a, b = span[c["side"]]; win.append((s - a, s - b))
            elif c["side"] == "during":
                win.append((s, e))
            else:
                win.append((e, e + dt.timedelta(hours=30)))
        hit = set()
        acts = ("modified", "opened", "created", "printed", "sent", "saved_from_mail", "saved_from_messenger", "downloaded")
        said = [t for t in (c.get("types") or []) if t in by_type]   # the remembered action, when the record keeps that kind
        if said and os.environ.get("EPI_ACT_VERB", "1") == "1":
            acts = said
        for a, b in win:
            hit |= _in_range(by_type, acts, a.isoformat(timespec="minutes"), (b + dt.timedelta(minutes=1)).isoformat(timespec="minutes"))
        return hit
    return None
