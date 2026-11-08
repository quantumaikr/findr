"""the reranker's cost on a Mac is the candidates times their length (11/9): the tail past the first ten may read a shorter
opening (CE_TAIL), and FINDR_SLOW says how many candidates were scored (a Mac's 3 s budget can stop before 30, #74)"""
import search


class Tok:
    """a stand-in tokenizer: one id per character"""
    seen = []

    def __call__(self, qs, ds, padding=False, truncation=True, max_length=512, return_tensors=None):
        Tok.seen += list(ds)
        import torch
        ids = [[1] * min(len(q) + len(d), max_length) for q, d in zip(qs, ds)]
        L = max(len(x) for x in ids); ids = [x + [0] * (L - len(x)) for x in ids]
        t = torch.tensor(ids)
        return type("B", (dict,), {"to": lambda self, dev: self})({"input_ids": t, "attention_mask": (t > 0).long()})


class Model:
    device = type("D", (), {"type": "cuda"})()

    def __call__(self, input_ids, attention_mask):
        return type("O", (), {"logits": attention_mask.sum(1, keepdim=True).float() / 1000})()


def test_tail_reads_a_shorter_opening(monkeypatch):
    res = [{"name": f"f{i}", "path": f"/d/f{i}", "head": "가" * 900} for i in range(14)]
    monkeypatch.setattr(search, "CE_CACHE_MAX", 0)
    monkeypatch.setenv("CE_TAIL", "1")
    Tok.seen = []
    search._ce_score("q", res, "d", Tok(), Model(), True)
    lens = [len(s) for s in Tok.seen]
    assert max(lens[:10]) > 700 and max(lens[10:]) < 260   # the first ten as before, the tail shorter
    monkeypatch.setenv("CE_TAIL", "0")
    Tok.seen = []
    search._ce_score("q", res, "d", Tok(), Model(), True)
    assert min(len(s) for s in Tok.seen) > 700              # off: every candidate reads the same opening


def test_slow_line_counts_scored_candidates(capsys):
    search._ST.d = {"ce_scores": 2.5, "ce_n": 30.0}
    search._slow_line({"results": [1, 2]}, "xce", 2.7)
    out = capsys.readouterr().out
    assert '"ce_n": 30' in out and '"ce_n": 30000' not in out
    search._ST.d = None


def test_tail_is_on_by_default_on_an_apple_gpu_only(monkeypatch):
    res = [{"name": f"f{i}", "path": f"/d/f{i}", "head": "가" * 900} for i in range(14)]
    monkeypatch.setattr(search, "CE_CACHE_MAX", 0)
    monkeypatch.delenv("CE_TAIL", raising=False)
    for dev, short in (("mps", True), ("cuda", False)):
        monkeypatch.setattr(search, "DEVICE", dev)
        Tok.seen = []
        search._ce_score("q", res, "d", Tok(), Model(), True)
        assert (max(len(s) for s in Tok.seen[10:]) < 260) is short
