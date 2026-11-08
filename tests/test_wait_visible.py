"""(1) the reranker is loaded once even when the warm-up (now outside the search lock) and a question ask for it together;
(2) the warm-up's reranker pass does not hold the search lock (Mac 에어: fast mode waited 3.4 s); (3) the reported time includes
the wait (13.9 s shown as 5.1)"""
import threading
import time
import types

import torch

import search


class _Tok:
    def __call__(self, qs, docs, **kw):
        return types.SimpleNamespace(to=lambda dev: {"input_ids": torch.zeros(len(qs), 4, dtype=torch.long)})


class _Model:
    device = torch.device("cpu")

    def __call__(self, **b):
        return types.SimpleNamespace(logits=torch.zeros(b["input_ids"].shape[0], 1))


def test_reranker_loads_once_under_concurrency(monkeypatch, tmp_path):
    d = str(tmp_path / "rank")
    monkeypatch.setenv("CE_DIR", d)
    monkeypatch.delitem(search.CE, d, raising=False)
    loads = []

    def slow_load(d_):
        loads.append(d_); time.sleep(0.3); search.CE[d_] = (_Tok(), _Model())
    monkeypatch.setattr(search, "_ce_load", slow_load)
    docs = [{"name": "a.txt", "path": "/x/a.txt", "head": "본문", "summary": ""}]
    ts = [threading.Thread(target=search.ce_scores, args=("견적", docs)) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert loads == [d]


def test_warm_reranker_pass_is_outside_the_search_lock():
    src = search.Path(search.__file__).read_text(encoding="utf-8")
    i = src.index('ce_scores("지난달 회의 자료"'); j = src.index("with SEARCH_LOCK:", i - 400)
    assert j > i, "the warm-up's reranker pass must come before the search lock is taken"


def test_reported_time_includes_the_wait():
    src = search.Path(search.__file__).read_text(encoding="utf-8")
    assert 'res["wait_ms"] = int(wait_ * 1000); res["ms"] = int(res.get("ms") or 0) + res["wait_ms"]' in src
