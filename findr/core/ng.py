"""Understanding-first search (2026-11-06): the model reads the question into a PLAN; code executes the plan over the
fact cards and the knowledge layer (kg.py) and explains the answer or why nothing fits. Similarity search is only used to
order what the plan leaves, or as a fallback when the plan says nothing usable.
    from ng import NG; ng = NG(finder); ng.search(q, prev=None)
"""
import datetime as dt
import json
import re

import search as S

PLAN_PROMPT = """오늘은 {today}입니다. 회사 PC에서 파일을 찾는 질문을 읽고, 찾는 방법을 JSON으로만 쓰세요. 질문에 없는 것은 null.
{{
 "종류": 찾는 문서 종류 목록 ({kinds} 중에서, 질문에 나온 것만),
 "거래처": 상대 회사 이름 또는 null,
 "사람": 작성자·담당자 이름 또는 null,
 "사람역할": 이름 대신 "퇴사한 사람"처럼 말하면 "퇴사자", "업무를 넘겨받은 사람"이면 "인계자", 아니면 null,
 "시기": 질문에 있는 시기 표현을 그대로(예: "작년 가을", "3월") 또는 null,
 "상태": "최종"(서명본·최종본) 또는 "초안" 또는 null,
 "금액": 질문에 있는 금액 표현을 그대로(예: "1,980만원쯤", "5천만원 넘는") 또는 null,
 "관계": 다른 문서를 거쳐야 찾을 수 있으면 다음 중 하나, 아니면 null:
   "회의후" (어떤 회의·협의 결정 다음에 나온 문서),
   "견적에서발주" (어떤 견적으로 나간 발주서),
   "견적에서명세" (어떤 견적 건의 거래명세서·납품),
   "발주에서명세" (어떤 발주 건의 거래명세서),
 "기준번호": 질문에 나온 문서 번호(예: Q-2026-123, PO-2025-4567) 또는 null,
 "품목": 질문에 나온 품목 이름 또는 null,
 "버전": "최신"(제일 마지막), "직전"(바로 전 버전), "처음"(맨 처음 초안), "서명전마지막"(서명 직전) 중 하나 또는 null,
 "이전질문참조": 질문이 "그거", "그 전 버전"처럼 앞 질문을 가리키면 true,
 "주제": 위에 담지 못한 핵심 내용 단어(예: "임대차", "ERP 연동") 또는 null
}}
{prev}질문: {q}"""


def money_by_code(t):
    """(min, max) in won from a money phrase, computed by code (the models mis-read units and counts)"""
    if not t:
        return None
    s = t.replace(",", "").replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)(억|천만|백만|만|천)?원?", s)
    if not m or not (m.group(2) or "원" in s[m.end() - 1:m.end()]):
        return None
    unit = {"억": 1e8, "천만": 1e7, "백만": 1e6, "만": 1e4, "천": 1e3, None: 1}[m.group(2)]
    v = float(m.group(1)) * unit
    m2 = re.match(r"(\d+)억(\d+)천?만?", s)
    if m2 and m2.group(2):   # "3억5천" / "3억 5천만"
        v = int(m2.group(1)) * 1e8 + int(m2.group(2)) * (1e7 if "천" in s else 1e4)
    if re.search(r"넘|이상|초과|이 넘", s):
        return int(v), None
    if re.search(r"이하|미만|안\s*되|도안|안하|아래", s):
        return None, int(v)
    w = 0.3 if re.search(r"쯤|정도|내외|약|가량|대", s) else 0.1
    return int(v * (1 - w)), int(v * (1 + w))


class NG:
    def __init__(self, f):
        self.f = f; f.load_facts(); con = f.con
        self.person = {r[1]: dict(name=r[0], day=r[2], other=r[3]) for r in con.execute("SELECT name, role, day, other FROM person")}
        self.cites = [(s, d) for s, d in con.execute("SELECT src, dst FROM rel WHERE rel='cites'")]
        self.follows = [(s, d) for s, d in con.execute("SELECT src, dst FROM rel WHERE rel='follows_meeting'")]
        self.own = {n: f_ for f_, n in con.execute("SELECT file, num FROM docnum WHERE role='own'")}
        self.vchain, self.csubj, self.cday = {}, {}, {}
        cols = [r[1] for r in con.execute("PRAGMA table_info(vchain)")]
        q_ = "SELECT chain, file, pos, day, signed, subject FROM vchain" if "subject" in cols else "SELECT chain, file, pos, day, signed, '' FROM vchain"
        for c, f_, pos, day, signed, subj in con.execute(q_):
            self.vchain[f_] = (c, pos, signed); self.csubj[c] = subj or ""; self.cday[c] = max(self.cday.get(c, ""), day or "")
        self.text = {fid: t or "" for fid, t in con.execute("SELECT file, group_concat(text,' ') FROM chunks WHERE n>=0 GROUP BY file")}

    def plan(self, q, prev=None):
        p = PLAN_PROMPT.format(today=S.today_now().date().isoformat(), kinds=", ".join(S.FKINDS), q=q,
                               prev=f"앞 질문: {prev}\n" if prev else "")
        try:
            j = json.loads(S.chat_json(p, 260))
        except Exception:
            j = {}
        if not isinstance(j, dict):
            return {}
        for key in ("거래처", "사람", "사람역할", "시기", "상태", "금액", "관계", "기준번호", "품목", "버전", "주제"):   # one value each
            v = j.get(key)
            if isinstance(v, list):
                v = next((x for x in v if isinstance(x, str) and x.strip()), None)
            j[key] = v.strip() if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none") else None
        k = j.get("종류")
        j["종류"] = [k] if isinstance(k, str) else [x for x in (k or []) if isinstance(x, str)]
        j["이전질문참조"] = bool(j.get("이전질문참조")) and j.get("이전질문참조") not in ("false", "False")
        return j

    def kind_of(self, f):
        return self.f.facts[f]["kind"]

    def partner_ok(self, f, pn):
        pn = re.sub(r"\(주\)|\s", "", pn); fa = self.f.facts[f]
        return any(pn in re.sub(r"\(주\)|\s", "", x) or re.sub(r"\(주\)|\s", "", x) in pn for x in fa["partners"] if len(x) >= 2) or pn in fa["text"]

    def execute(self, pl, q, prev_answer=None):
        F = self.f.facts; why = []
        cand = set(F)
        kinds = [k for k in (pl.get("종류") or []) if k in S.FKINDS] if isinstance(pl.get("종류"), list) else []
        if S.PROFILE == "pc":   # on a researcher's PC the kind is guessed, not said: filtering by it lost answers (2026-11-05/06)
            kinds = []
        rel = pl.get("관계"); num = pl.get("기준번호"); item = pl.get("품목")
        # anchors and relations first (they decide the target set)
        if rel in ("견적에서발주", "견적에서명세", "발주에서명세"):
            src_kind = "발주서" if rel.startswith("발주") else "견적서"
            anchors = {self.own[num]} if num and num in self.own else {f for f in F if F[f]["kind"] == src_kind
                                                                      and (not pl.get("거래처") or self.partner_ok(f, pl["거래처"]))
                                                                      and (not item or item in F[f]["text"])}
            if num and num not in self.own:
                return [], f"문서 번호 {num}인 문서가 없습니다.", why
            if not anchors:
                return [], f"기준이 되는 {src_kind}를 찾지 못했습니다.", why
            why.append(f"기준 {src_kind} {len(anchors)}건")
            hop = {s for s, d in self.cites if d in anchors}
            if rel == "견적에서명세":
                hop = {s for s, d in self.cites if d in hop}
            want = "발주서" if rel == "견적에서발주" else "거래명세서"
            cand = {f for f in hop if F.get(f, {}).get("kind") == want}
            if not cand:
                return [], f"그 {src_kind}에 이어진 {want}가 없습니다.", why
            kinds = []
        elif rel == "회의후":
            meetings = {f for f in F if F[f]["kind"] == "회의록" and (not pl.get("거래처") or self.partner_ok(f, pl["거래처"]))}
            topic = pl.get("주제") or ""
            if "단가" in q or "단가" in topic:
                meetings = {f for f in meetings if "단가" in self.text.get(f, "")}
            if not meetings:
                return [], f"{pl.get('거래처') or ''} 관련 그런 회의 기록이 없습니다.".strip(), why
            cand = {s for s, d in self.follows if d in meetings}
            why.append(f"관련 회의 {len(meetings)}건")
            if not cand:
                return [], "그 회의 결정을 반영한 문서가 없습니다.", why
        # people by role (the fact lives only in the HR notice)
        name = pl.get("사람"); role = pl.get("사람역할")
        if re.search(r"넘겨받|인계|이어받|후임|물려받", q):   # "X 퇴사하고 업무 넘겨받은 사람" is the successor, not X
            role = "인계자"
            if name and "left" in self.person and name == self.person["left"]["name"]:
                name = None
        elif re.search(r"퇴사|그만둔", q) and not name:   # not "나간": "견적으로 나간 발주서" is not about a person
            role = role or "퇴사자"
        after = None
        if role == "퇴사자" and "left" in self.person:
            name = self.person["left"]["name"]; why.append(f"퇴사자 = {name}(인사 안내)")
        elif role == "인계자" and "successor" in self.person:
            name = self.person["successor"]["name"]; after = self.person["successor"]["day"]; why.append(f"인계자 = {name}, {after} 이후")
        c = {}
        if kinds:
            c["kinds"] = kinds + (["비밀유지계약서"] if "계약서" in kinds else [])
        if pl.get("거래처") and rel not in ("견적에서발주", "견적에서명세", "발주에서명세"):
            c["partner"] = re.sub(r"\(주\)|\s", "", pl["거래처"])
        w = S.period_by_code(pl.get("시기") or q, S.today_now().date()) if (pl.get("시기") or re.search(r"작년|올해|월|봄|여름|가을|겨울", q)) else None
        if not w:   # relative days ("어제", "지난주", "3일 전", "6개월 전"): the ordinary rule parser computes those
            p0 = S.parse(q)
            if p0["since"]:
                w = (p0["since"].date().isoformat(), ((p0["until"] or S.today_now()) - dt.timedelta(seconds=1)).date().isoformat())
        if w:
            c["from"], c["to"] = w
        mon = money_by_code(pl.get("금액"))
        if mon:
            c["amin"], c["amax"] = mon
        if name:
            c["person"] = re.sub(r"\s|(대리|과장|님|씨)$", "", name)
        if pl.get("상태") in ("최종", "초안") and not pl.get("버전"):
            c["status"] = pl["상태"]
        cand = {f for f in cand if S.fact_ok(F[f], c)}
        if name and (role or re.search(r"맡|담당|하던|작성|쓴", q)):   # owner, not just an attendee: own PC folder or "작성/담당: name"
            nm = c.get("person", name)
            own = {f for f in cand if re.search(rf"/(PC|퇴사자백업)/{re.escape(nm)}", F[f]["path"])
                   or re.search(rf"(작성|담당|출장자|점검자|확인)\s*:\s*{re.escape(nm)}", self.text.get(f, ""))}
            if own:
                cand = own; why.append(f"{nm}이(가) 담당한 문서만")
        if after:
            cand = {f for f in cand if F[f]["date"] > after}
        # versions: choose within each contract chain the plan asks for
        ver = pl.get("버전")
        if ver:
            chains = {}
            for f in cand:
                if f in self.vchain:
                    chains.setdefault(self.vchain[f][0], []).append(f)
            # which contract does the question mean? compare the question's item/topic with each chain's contract subject
            # (written in the contract itself); among equally good matches the most recent contract comes first
            want = " ".join(x for x in (pl.get("품목"), pl.get("주제")) if x) or q
            def bigr(t):
                t = re.sub(r"\s", "", t)
                return {t[i:i + 2] for i in range(len(t) - 1)}
            wb = bigr(want)
            prev_chain = next((self.vchain[a][0] for a in (prev_answer or []) if a in self.vchain), None)
            hyb = {}
            members = {f2 for f2, v2 in self.vchain.items() if v2[0] in chains}
            if len(chains) > 1 and members:   # the earlier ordering (hybrid search over the versions) breaks ties
                p = S.parse(q); p["fids"] = members; p["since"] = p["until"] = None
                for rnk, x in enumerate(self.f._search(q, p, pl.get("주제") or p["text"], "", 50, 0)["results"]):
                    hyb.setdefault(self.vchain[x["fid"]][0], rnk)
            def ckey(kk):
                sb = bigr(self.csubj.get(kk, ""))
                ov = len(wb & sb) / max(1, len(sb)) if sb else 0
                return (kk == prev_chain, round(ov, 1), -hyb.get(kk, 999))
            order_k = sorted(chains, key=ckey, reverse=True)
            order_k += [kk for kk in chains if kk not in order_k]
            pick = []
            for k in order_k:
                fs = chains[k]
                allv = [(f2, v2) for f2, v2 in self.vchain.items() if v2[0] == k]
                maxpos = max(v[1] for _, v in allv)
                if ver == "최신":
                    tgt = maxpos
                elif ver == "처음":
                    tgt = 0
                elif ver == "직전":
                    base = max((self.vchain[a][1] for a in (prev_answer or []) if a in self.vchain and self.vchain[a][0] == k), default=maxpos)
                    tgt = base - 1
                else:   # 서명전마지막
                    uns = [v[1] for _, v in allv if v[2] == 0]
                    tgt = max(uns) if uns else -1
                pick += sorted(f2 for f2, v2 in allv if v2[1] == tgt and f2 not in pick)
            if chains:
                why.append(f"버전 '{ver}' (계약 묶음 {len(chains)}개 중 질문과 가장 맞는 것부터)")
                return pick, ("" if pick else "그 버전이 없습니다."), why
            why.append(f"버전 '{ver}'")
        why.append(f"조건 {json.dumps({k: v for k, v in c.items() if k != 'kinds'}, ensure_ascii=False)}" if c else "조건 없음")
        if not cand:
            return [], "조건에 맞는 파일이 없습니다.", why
        return sorted(cand), "", why

    def search(self, q, prev=None, k=10):
        import time
        t0 = time.time()
        prev_answer = None
        if prev:
            pp = self.plan(prev); ids, _, _ = self.execute(pp, prev)
            prev_answer = self.rank(prev, ids, pp)[:1]
        pl = self.plan(q, prev)
        if pl.get("이전질문참조") and prev and not any(pl.get(x) for x in ("거래처", "종류")):
            pp = self.plan(prev)   # inherit the earlier question's target, keep this turn's version request
            for key in ("종류", "거래처", "주제", "관계", "기준번호", "품목"):
                pl.setdefault(key, None)
                if not pl.get(key):
                    pl[key] = pp.get(key)
        ids, note, why = self.execute(pl, q, prev_answer)
        empty_plan = not any(pl.get(x) for x in ("종류", "거래처", "사람", "사람역할", "시기", "상태", "관계", "기준번호", "버전", "금액"))
        if empty_plan:   # the plan says nothing usable: fall back to ordinary search
            r = self.f.search(q, k=k, mode="xce"); r["plan"] = pl; r["why"] = ["계획 없음 → 일반 검색"]
            return r
        order = list(ids) if pl.get("버전") else self.rank(q, ids, pl)   # version picks come already ordered
        res = self.f.files_out(order[:k])
        return {"parsed": {"계획": json.dumps({x: y for x, y in pl.items() if y}, ensure_ascii=False)}, "results": res,
                "note": note, "why": why, "plan": pl, "ms": int(1000 * (time.time() - t0))}

    def rank(self, q, ids, pl):
        if len(ids) <= 1:
            return list(ids)
        if S.os.environ.get("NG_CE", "1") == "1" and len(ids) <= 60:
            # order what the plan left with the trained reranker (the plan decides WHICH files, the reranker the ORDER)
            rows = self.f.files_out(list(ids))
            hd = dict(self.f.con.execute(f"SELECT file, text FROM chunks WHERE n=0 AND file IN ({','.join('?' * len(rows))})",
                                         [x["fid"] for x in rows]).fetchall())
            for x in rows:
                x["head"] = hd.get(x["fid"]) or ""; x["summary"] = ""
            sc = S.ce_scores(q, rows, d=S.os.environ.get("NG_CE_DIR", str(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "rank")), nosumm=True)
            return [x["fid"] for _, x in sorted(zip(sc, rows), key=lambda t: -t[0])]
        topic = pl.get("주제")
        if topic or len(ids) > 10:
            p = S.parse(q); p["fids"] = set(ids); p["since"] = p["until"] = None
            r = self.f._search(q, p, topic or p["text"], "", 30, 0)
            ordered = [x["fid"] for x in r["results"]]
            return ordered + [f for f in sorted(ids, key=lambda f: self.f.facts[f]["date"], reverse=True) if f not in ordered]
        return sorted(ids, key=lambda f: self.f.facts[f]["date"], reverse=True)
