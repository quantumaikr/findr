"""query-understanding model at search time: request -> schema reading -> episodic cue dicts (same shape as episodic.cues()).
    QU(path).read(q) -> dict (schema, defaults filled)        QU(path).cues(q) -> list of cue dicts
Falls back to the rule parser when the model output does not parse."""
import json, re
from pathlib import Path
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

SYS = "파일 찾기 요청을 읽고 구조화된 해석을 JSON 한 줄로만 출력하세요. 비어 있는 항목은 쓰지 마세요."
KEYS = {"content": "", "kind": None, "file_type": None, "people": [], "groups": [], "channel": None, "actions": [], "when": None,
        "activity": None, "version": None, "last_editor": False, "revised_many": False}
RECV = {"saved_from_mail", "saved_from_messenger", "downloaded"}
CH2TYPE = {"mail": "saved_from_mail", "messenger": "saved_from_messenger", "download": "downloaded"}
ACT2TYPES = {"received": RECV, "sent": {"sent"}, "printed": {"printed"}, "modified": {"modified"}, "created": {"created"},
             "opened": {"opened"}, "renamed": {"renamed"}, "copied": {"copied"}}


class QU:
    def __init__(self, path, device="cuda"):
        self.tok = AutoTokenizer.from_pretrained(path)
        self.m = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32).to(device).eval()
        self.dev = device

    @torch.no_grad()
    def read(self, q):
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": q}]
        try:
            p = self.tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        except TypeError:
            p = self.tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        ids = self.tok(p, return_tensors="pt", add_special_tokens=False).to(self.dev)
        out = self.m.generate(**ids, max_new_tokens=200, do_sample=False)
        txt = self.tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
        m = re.search(r"\{.*\}", txt, re.S)
        y = json.loads(m.group(0)) if m else None
        return {**KEYS, **(y or {})} if y is not None else None

    def cues(self, q, action_dates=None):
        y = self.read(q)
        return None if y is None else to_cues(y, q, action_dates)


class QUServer:
    """the same model served by llama.cpp (gguf): fast on GPU, runs on CPU too"""
    def __init__(self, url="http://127.0.0.1:8093/v1/chat/completions"):
        self.url = url

    def read(self, q):
        import urllib.request
        body = json.dumps({"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": q}], "max_tokens": 200,
                           "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}).encode()
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(self.url, body, {"Content-Type": "application/json"}), timeout=10))
            txt = r["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", txt, re.S)
            return {**KEYS, **json.loads(m.group(0))} if m else None
        except Exception:
            return None

    def cues(self, q, action_dates=None):
        y = self.read(q)
        return None if y is None else to_cues(y, q, action_dates)


def to_cues(y, q, action_dates=None):
    """schema reading -> cue dicts for episodic.files_for (and the content part for the content path)"""
    out = []
    recv_only = "received" in (y.get("actions") or []) and "sent" not in (y.get("actions") or [])   # 'X한테 받아서': X is the sender
    for p in y.get("people") or []:
        role = {"from": "from", "to": "sent_to", "editor": "any", "author": "any"}.get(p.get("role"), None)
        if role == "sent_to" and recv_only:
            role = "from"
        if role is None:
            continue   # 'mentioned' = what the document is about: content, not history
        name = (p.get("name") or "").replace(" ", ""); title = p.get("title")
        c = {"kind": "person", "name": name, "title": title, "role": role, "text": (name + (" " + title if title else "")).strip()}
        if p.get("role") == "editor" and y.get("last_editor"):
            c["last"] = True
        out.append(c)
    for g in y.get("groups") or []:
        if g.get("role") in ("to", "from"):   # a department, client, portal or platform that sent or received the file
            out.append({"kind": "group", "name": (g.get("name") or "").replace(" ", ""), "role": "sent_to" if g["role"] == "to" and not recv_only else "from",
                        "text": g.get("name") or ""})
    acts = set(y.get("actions") or [])
    ch = y.get("channel")
    if ch in CH2TYPE and "sent" not in acts:
        out.append({"kind": "source", "type": CH2TYPE[ch], "text": ch})
    for a in acts - {"received"}:
        if a in ("sent", "printed", "renamed", "copied"):
            out.append({"kind": "action", "type": a, "text": a})
    w = y.get("when") or {}
    if w and w.get("applies_to") == "action" and action_dates:
        rng = action_dates(w.get("text") or q)
        if rng:
            types = set()
            for a in acts or {"modified"}:
                types |= ACT2TYPES.get(a, set())
            out.append({"kind": "when", "range": (rng[0].isoformat(), rng[1].isoformat()), "types": sorted(types), "text": w.get("text") or rng[2]})
    act = y.get("activity") or {}
    if act and act.get("event"):
        rel = {"day_before": "daybefore"}.get(act.get("relation"), act.get("relation") or "before")
        types = set()
        for a in acts:
            types |= ACT2TYPES.get(a, set())
        if not types and action_dates is not None:
            import episodic as _E
            types = _E.verb_types(q)
        out.append({"kind": "activity", "event": act["event"], "side": rel, "words": act.get("words") or [], "text": act["event"], "types": sorted(types)})
    if y.get("revised_many"):
        out.append({"kind": "revised", "text": "여러 번"})
    return out
