"""the real-file evaluation tool: ranks across path spellings and folded copies, the shared summary carries numbers only"""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("real_eval", Path(__file__).parents[1] / "tools" / "real_eval.py")
RE = importlib.util.module_from_spec(spec); spec.loader.exec_module(RE)


def test_rank_across_spellings_and_copies():
    res = [{"path": r"C:\work\a\x.pdf"}, {"path": "/mnt/c/work/a/y.pdf", "copies": [r"D:\old\z.pdf"]}]
    assert RE.rank_of(res, ["/mnt/c/work/a/x.pdf"]) == 1          # a Windows path and its WSL spelling are one file
    assert RE.rank_of(res, [r"D:\old\z.pdf"]) == 2                 # a folded copy counts as the answer
    assert RE.rank_of(res, ["/elsewhere.pdf"]) is None


def test_summary_and_compare_hold_no_text():
    rows = [{"id": "A1", "kind": "known", "rank": 1, "ms": 300, "q": "비밀 질문"}, {"id": "A2", "kind": "known", "rank": 4, "ms": 900},
            {"id": "N1", "kind": "none", "note": "없음", "ms": 200}]
    s = RE.summarize(rows)
    assert (s["hit1"], s["top3"], s["top10"], s["none_said"], s["p50_ms"]) == (1, 1, 2, 1, 300)
    assert "비밀" not in json.dumps(s, ensure_ascii=False)
    prev = [{"id": "A1", "kind": "known", "rank": 2}, {"id": "A2", "kind": "known", "rank": 4}]
    assert RE.compare(rows, prev) == {"better": ["A1"], "worse": [], "same": 1}
