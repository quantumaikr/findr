"""One-page project summary (2026-11-06): the local model (Qwen3-1.7B, planner LoRA off) reads the opening text of a project's
latest files and writes 3-4 Korean lines, each citing its source file [n]. Code then checks every number in a line against
the cited file's text (and the line's file must exist); a line with an unverifiable number is dropped, never 'fixed'.
Cached by project folder + newest mtime in summaries.json next to the DB."""
import json, re, sqlite3, urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8091/v1/chat/completions"
PROMPT = ("다음은 '{name}' 프로젝트의 최근 파일들입니다. 파일을 하나씩 나열하지 말고, 프로젝트 전체를 처음 보는 동료에게 설명하는 요약을 한국어로 정확히 3줄 쓰세요.\n"
          "- 1줄: 이 프로젝트가 무엇을 위한 일인지 (여러 파일을 종합해서)\n- 2줄: 가장 최근에 진행된 일과 현재 상태\n- 3줄: 핵심 숫자·결정·결과 하나 이상\n"
          "규칙: 줄을 파일 이름으로 시작하지 말 것. 각 줄 끝에 근거 파일 번호 [n] (여러 개면 [1][3]). 파일에 적힌 숫자만 쓰고 추측 금지. 각 줄은 '- '로 시작.\n\n{files}")
NUM = re.compile(r"\d[\d,.]*\s*%?")


def _ask(prompt):
    body = {"messages": [{"role": "user", "content": prompt + " /no_think"}], "temperature": 0, "max_tokens": 360, "lora": [{"id": 0, "scale": 0.0}]}
    t = json.load(urllib.request.urlopen(urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=90))
    return re.sub(r"<think>.*?</think>", "", t["choices"][0]["message"]["content"], flags=re.S)


def _norm(x):
    return re.sub(r"[\s,]", "", x).rstrip(".")


def summarize(db, project, nfiles=6, nchar=380):
    """project: one entry of explorer.build()["projects"]. Returns {"lines": [{"text", "src": [fid]}], "dropped": n, "files": [...]}"""
    cache_p = Path(db).with_name("summaries.json")
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    key = f'{project["dir"]}|{project["last"]}'
    if key in cache:
        return cache[key]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    fs = [f for f in project["files"] if not f.get("newer")][:nfiles]
    texts = []
    for f in fs:
        t = " ".join(x for (x,) in con.execute("SELECT text FROM chunks WHERE file=? ORDER BY n LIMIT 2", (f["fid"],)) if x)
        texts.append(t)
    listing = "\n".join(f"[{i+1}] {f['name']}" + (f" ({f['title']})" if f.get("title") else "") + f"\n{t[:nchar]}" for i, (f, t) in enumerate(zip(fs, texts)))
    out = _ask(PROMPT.format(name=project["name"], files=listing))
    lines, dropped = [], 0
    for raw in out.splitlines():
        raw = raw.strip()
        if not raw.startswith(("-", "•", "*")):
            continue
        refs = [int(n) for n in re.findall(r"\[(\d+)\]", raw) if 1 <= int(n) <= len(fs)]
        text = re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]", "", raw.lstrip("-•* ")).strip()
        if not text or not refs:
            dropped += 1; continue
        src = " ".join(texts[n - 1] for n in refs); src_n = _norm(src)
        if any(_norm(m) not in src_n for m in NUM.findall(text) if len(_norm(m)) >= 2):   # a number the cited files do not contain
            dropped += 1; continue
        lines.append({"text": text, "src": [fs[n - 1]["fid"] for n in refs]})
    res = {"lines": lines[:4], "dropped": dropped, "files": [{"fid": f["fid"], "name": f["name"]} for f in fs]}
    cache[key] = res; cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return res
