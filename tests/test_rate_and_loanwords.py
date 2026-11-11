"""(1) on a CPU the reranker's rate is measured once and kept, so the same question gets the same candidates (#66: rank 1, then
rank 8 a moment later); (2) Korean words for English file names common on students' and developers' PCs (#66)"""
import types

import torch

import search


class _Tok:
    def __call__(self, qs, docs, **kw):
        n = len(qs)
        return types.SimpleNamespace(to=lambda dev: {"input_ids": torch.zeros(n, 4, dtype=torch.long)})


class _Model:
    device = torch.device("cpu")

    def __call__(self, **b):
        return types.SimpleNamespace(logits=torch.zeros(b["input_ids"].shape[0], 1))


def test_cpu_rate_is_measured_once(monkeypatch, tmp_path):
    d = str(tmp_path / "rank")
    monkeypatch.setenv("CE_DIR", d)
    monkeypatch.setitem(search.CE, d, (_Tok(), _Model()))
    monkeypatch.setattr(search, "CE_RATE", {})
    docs = [{"name": f"{i}.txt", "path": f"/x/{i}.txt", "head": "본문", "summary": ""} for i in range(15)]
    search._QT[0] = __import__("time").time()
    search.ce_scores("견적서", docs)
    first = search.CE_RATE[d]
    for _ in range(3):
        search._QT[0] = __import__("time").time()
        search.ce_scores("견적서", docs)
    assert search.CE_RATE[d] == first, "the rate must not follow the questions"


def test_loanwords_bring_english_names_first():
    for ko, en in [("윤곽선", "contour"), ("블러", "blur"), ("스무딩", "smoothing"), ("크롤링", "crawl"), ("셀레니움", "selenium"),
                   ("코드", "code"), ("노트북", "notebook")]:
        assert en in search.XLING[ko], ko
    res = [{"name": n, "path": "/s/" + n} for n in ("lecture_notes.pdf", "image_blurring_practice.ipynb", "readme.md")]
    j = search.xling_name_first("블러 처리 실습", res)
    assert j == 1, j


def test_warm_up_runs_the_reranker_at_full_size():
    src = (search.Path(search.__file__)).read_text(encoding="utf-8")
    assert 'for _ in range(NCAND)])' in src and "ce_scores(\"지난달 회의 자료\"" in src


def test_apple_gpu_stops_at_the_question_budget(monkeypatch, tmp_path):
    """Apple GPU: steps of 10 candidates until the question's budget is spent; the first 10 always (field: 7-12 s per question
    while indexing shared the GPU). A fake clock: a slow CI runner took longer than the budget for the first step alone"""
    clock = [1000.0]
    monkeypatch.setattr(search, "time", types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
    calls = []

    class _Slow(_Model):
        device = types.SimpleNamespace(type="mps")

        def __call__(self, **b):
            calls.append(b["input_ids"].shape[0]); clock[0] += 0.2
            return types.SimpleNamespace(logits=torch.ones(b["input_ids"].shape[0], 1))

    d = str(tmp_path / "rank")
    monkeypatch.setenv("CE_DIR", d)
    monkeypatch.setenv("CE_TOTAL_MPS", "0.5")
    monkeypatch.setitem(search.CE, d, (_Tok(), _Slow()))
    docs = [{"name": f"{i}.txt", "path": f"/x/{i}.txt", "head": "본문", "summary": ""} for i in range(30)]
    search._QT[0] = clock[0]
    sc = search.ce_scores("견적서", docs)
    assert calls == [10, 10] and len(sc) == 30   # 0.2 s; 0.4 s + 0.2 s for a third step > 0.5 s: stopped after 20
    assert max(sc[20:]) < min(sc[:20])   # unscored ones stay below, in their first-stage order
    calls.clear(); search.CE_CACHE.clear(); search._QT[0] = clock[0] - 10   # (scores not cached) budget already spent (a long first stage): still the first 10
    search.ce_scores("견적서", docs)
    assert calls == [10]


def test_scores_are_cached_by_question_and_text(monkeypatch, tmp_path):
    """the same question and the same candidate text are scored once (repeat questions, the page asking again, repair passes);
    an edited file has a new text and is scored anew; the number of candidates scored stays the same"""
    seen = []

    class _Count(_Model):
        def __call__(self, **b):
            seen.append(b["input_ids"].shape[0])
            return types.SimpleNamespace(logits=torch.arange(b["input_ids"].shape[0], dtype=torch.float).view(-1, 1))

    d = str(tmp_path / "rank")
    monkeypatch.setenv("CE_DIR", d)
    monkeypatch.setitem(search.CE, d, (_Tok(), _Count()))
    monkeypatch.setattr(search, "CE_RATE", {})
    # every candidate scored on every call: the CPU's count (cpu_candidates, 5 at most) is set by the speed measured in the
    # first call, and a slow test machine (CI) then left the sixth to the unscored tail on the second call
    monkeypatch.setattr(search, "cpu_candidates", lambda d: 100)
    search.CE_CACHE.clear()
    docs = [{"name": f"{i}.txt", "path": f"/x/{i}.txt", "head": "본문", "summary": ""} for i in range(6)]
    import time
    search._QT[0] = time.time(); first = search.ce_scores("견적서", docs)
    search._QT[0] = time.time(); again = search.ce_scores("견적서", docs)
    assert again == first and seen == [6]   # nothing new to score the second time
    docs[2] = {**docs[2], "head": "고친 본문"}
    search._QT[0] = time.time(); search.ce_scores("견적서", docs)
    assert seen == [6, 1]   # only the edited file
    search._QT[0] = time.time(); search.ce_scores("계약서", docs)
    assert seen[-1] == 6   # another question: its own scores
