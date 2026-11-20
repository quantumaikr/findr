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


def test_a_miss_says_what_the_first_file_is_to_the_answer(tmp_path):
    def put(rel, text):
        p = tmp_path / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding="utf-8"); return p.as_posix()
    ans = put("a/보고서_v2.txt", "둘째 판")
    assert RE.miss_kind(put("a/보고서_v1.txt", "첫 판"), [ans]) == "version"     # the same document, another version
    assert RE.miss_kind(put("b/보고서_v2.txt", "둘째 판"), [ans]) == "copy"       # the same bytes elsewhere
    assert RE.miss_kind(put("a/예산.txt", "예산"), [ans]) == "folder"
    assert RE.miss_kind(put("c/메모.txt", "메모"), [ans]) == "other"
    assert RE.miss_kind(None, [ans]) == "empty"
    s = RE.summarize([{"id": "A1", "kind": "known", "rank": 2, "miss": "version"}, {"id": "A2", "kind": "known", "rank": 1}])
    assert s["miss"] == {"copy": 0, "version": 1, "folder": 0, "other": 0, "empty": 0}


def test_summary_and_compare_hold_no_text():
    rows = [{"id": "A1", "kind": "known", "rank": 1, "ms": 300, "q": "비밀 질문"}, {"id": "A2", "kind": "known", "rank": 4, "ms": 900},
            {"id": "N1", "kind": "none", "note": "없음", "ms": 200}]
    s = RE.summarize(rows)
    assert (s["hit1"], s["top3"], s["top10"], s["none_said"], s["p50_ms"]) == (1, 1, 2, 1, 300)
    assert "비밀" not in json.dumps(s, ensure_ascii=False)
    prev = [{"id": "A1", "kind": "known", "rank": 2}, {"id": "A2", "kind": "known", "rank": 4}]
    assert RE.compare(rows, prev) == {"better": ["A1"], "worse": [], "same": 1}
