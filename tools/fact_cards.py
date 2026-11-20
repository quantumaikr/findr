"""What the index already knows about each file, as facts (file, relation, value): the first half of making questions with
known answers (the KG2KV idea from AtlasKV, 2025: a fact with one side masked is a question whose answer is that side).
    python tools/fact_cards.py INDEX.db --out FACTS.jsonl [--limit N]     (findr's own Python; a copy of the index is safest)

Relations, all read from what the index holds (nothing is opened or re-read, no model runs):
  term          one of the file's characteristic words (relations.terms: tf-idf over the text findr already read), with
                its 'rank': questions name a file by its first few, and the rest tell which other files the words also fit
  ext, folder   its format and the name of its folder
  date          its modified day
  author, last_by   from the document's own properties (traces: docmeta)
  ev:<type>     a memory trace (traces: events) -- saved_from_mail, saved_from_messenger, downloaded, sent, printed, renamed,
                copied, created, modified, opened -- with the person or source and the time
  version       where it stands in its version family (the engine's own grouping, search.family): latest, before_latest
                or first, by modified time; 'family' names the group so the other members can be hard negatives

FACTS.jsonl holds paths, names and people, so it stays on this PC. What is printed is counts per relation only.
The index is opened read-only: terms not cached yet are worked out and not stored."""
import argparse
import collections
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "findr" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

TERMS = 10   # characteristic words per file (relations.TERMS_K): the first ones name it, all of them judge ambiguity
EVENTS = ("saved_from_mail", "saved_from_messenger", "downloaded", "sent", "printed", "renamed", "copied", "created", "modified",
          "opened")


def connect(db):
    """the index, read-only (an engine or indexer may hold it)"""
    return sqlite3.connect(f"file:{Path(db).expanduser().resolve().as_posix()}?mode=ro", uri=True)


def _has(con, table):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone())


def _family_fn():
    try:
        import search
        return search.family
    except Exception:   # noqa: BLE001  (without the engine's packages there are no version facts)
        return None


def _day(t):
    return dt.datetime.fromtimestamp(float(t)).date().isoformat() if t else None


def facts(con, limit=None, family=None, terms=None):
    """fact dicts for the person's files (tier 1 and 3, as the trace harvester reads), file by file"""
    import relations
    family = family if family is not None else _family_fn()
    terms = terms or relations.terms
    rows = con.execute("SELECT id, path, ext, mtime FROM files WHERE tier IN (1, 3) ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    meta = {r[0]: r[1:] for r in con.execute("SELECT file, author, last_by FROM docmeta")} if _has(con, "docmeta") else {}
    evs = collections.defaultdict(list)
    if _has(con, "events"):
        for f, typ, t, person, source in con.execute("SELECT file, type, time, person, source FROM events ORDER BY time"):
            if typ in EVENTS:
                evs[f].append((typ, t, person, source))
    fams = collections.defaultdict(list)
    out = []
    for fid, path, ext, mtime in rows:
        base = {"file": fid, "path": path}
        for i, w in enumerate(terms(con, fid, k=TERMS)[:TERMS]):
            out.append({**base, "rel": "term", "value": w, "rank": i})
        if ext:
            out.append({**base, "rel": "ext", "value": ext.lower().lstrip(".")})
        parent = path.replace("\\", "/").rsplit("/", 2)
        if len(parent) == 3 and parent[1]:
            out.append({**base, "rel": "folder", "value": parent[1]})
        if mtime:
            out.append({**base, "rel": "date", "value": _day(mtime), "time": _day(mtime)})
        author, last_by = meta.get(fid, (None, None))
        if author:
            out.append({**base, "rel": "author", "value": author})
        if last_by and last_by != author:
            out.append({**base, "rel": "last_by", "value": last_by})
        for typ, t, person, source in evs.get(fid, []):
            out.append({**base, "rel": "ev:" + typ, "value": person or source or "", "time": (t or "")[:10] or None,
                        "source": source or None})
        if family:
            key = family({"path": path})
            if key and key[1]:
                fams[key].append((mtime or 0, fid, path))
    for key, members in fams.items():
        if len(members) < 2:
            continue
        members.sort()
        name = "|".join(key)
        pos = {members[-1][1]: "latest", members[-2][1]: "before_latest"}
        if len(members) >= 3:
            pos[members[0][1]] = "first"
        for _, fid, path in members:
            if fid in pos:
                out.append({"file": fid, "path": path, "rel": "version", "value": pos[fid], "family": name, "members": len(members)})
    return out


def counts(fs):
    """what may be shared: how many files and facts, per relation (no values, no paths)"""
    by = collections.Counter(f["rel"] for f in fs)
    return {"files": len({f["file"] for f in fs}), "facts": len(fs), "by_rel": dict(sorted(by.items()))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db"); ap.add_argument("--out", required=True); ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    fs = facts(connect(a.db), limit=a.limit)
    with open(a.out, "w", encoding="utf-8") as f:   # names and paths stay here, on this PC
        for x in fs:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    rep = counts(fs)
    print(json.dumps(rep, ensure_ascii=False))
    return rep


if __name__ == "__main__":
    main()
