"""reranking the first NCAND candidates keeps the rest below them in first-stage order (#66 rerun: with NCAND 5 on a CPU the page
asked for 10 and got 5)"""
import types

import torch

import search


class _Tok:
    def __call__(self, qs, docs, **kw):
        return types.SimpleNamespace(to=lambda dev: {"input_ids": torch.zeros(len(qs), 4, dtype=torch.long)})


class _Model:
    device = torch.device("cuda") if False else torch.device("meta")

    def __call__(self, **b):
        n = b["input_ids"].shape[0]
        return types.SimpleNamespace(logits=torch.arange(n, dtype=torch.float32).view(n, 1))   # later candidates score higher


def test_top_scores_only_the_first_and_keeps_the_rest(monkeypatch, tmp_path):
    d = str(tmp_path / "rank")
    monkeypatch.setitem(search.CE, d, (_Tok(), _Model()))
    docs = [{"name": f"{i}.txt", "path": f"/x/{i}.txt", "head": "본문", "summary": ""} for i in range(10)]
    sc = search.ce_scores("견적", docs, d=d, top=5)
    assert len(sc) == 10, "one score per candidate"
    assert min(sc[:5]) > max(sc[5:]), "the unscored ones stay below the scored ones"
    assert sc[5:] == sorted(sc[5:], reverse=True), "and keep their first-stage order"


def test_search_fetches_at_least_k_for_the_reranker():
    src = search.Path(search.__file__).read_text(encoding="utf-8")
    assert 'max(NCAND, k) if rer == "ce"' in src and "ce_scores(q, r[\"results\"], top=NCAND)" in src
