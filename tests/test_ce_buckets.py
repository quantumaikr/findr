"""fixed input shapes for the reranker (CE_BUCKETS): the same scores as one length per batch, padded to a few lengths and a
full batch so the Apple GPU reuses its execution graphs (Macs, 11/9); and the count of scored candidates in FINDR_SLOW"""
import search


class Tok:
    """a stand-in tokenizer: one id per character, padded as asked"""
    def __call__(self, qs, ds, padding=False, truncation=True, max_length=512, return_tensors=None):
        ids = [[1] * min(len(q) + len(d), max_length) for q, d in zip(qs, ds)]
        if padding == "max_length":
            ids = [x + [0] * (max_length - len(x)) for x in ids]
        elif padding:
            L = max(len(x) for x in ids); ids = [x + [0] * (L - len(x)) for x in ids]
        out = {"input_ids": ids}
        if return_tensors:
            import torch
            t = torch.tensor(ids); out = {"input_ids": t, "attention_mask": (t > 0).long()}
            return type("B", (dict,), {"to": lambda self, dev: self})(out)
        return out


class Model:
    device = type("D", (), {"type": "cpu"})()
    shapes = []

    def __call__(self, input_ids, attention_mask):
        import torch
        Model.shapes.append(tuple(input_ids.shape))
        return type("O", (), {"logits": attention_mask.sum(1, keepdim=True).float() / 100})()   # depends on real length only


def test_buckets_give_the_same_scores_in_fixed_shapes(monkeypatch):
    res = [{"name": f"f{i}", "path": f"/d/f{i}", "head": "x" * (20 + 13 * i)} for i in range(7)]
    monkeypatch.setattr(search, "CE_CACHE_MAX", 0)
    monkeypatch.setattr(search, "BUCKETS", [])
    monkeypatch.setattr(search, "CE_RATE", {})   # the CPU time budget would learn a rate and score fewer the second time
    Model.shapes = []
    a = search._ce_score("q", res, "d", Tok(), Model(), True)
    monkeypatch.setattr(search, "BUCKETS", [64, 128, 384]); search.CE_RATE.clear()
    monkeypatch.setenv("CE_STEP_MPS", "10")
    Model.shapes = []
    b = search._ce_score("q", res, "d", Tok(), Model(), True)
    assert max(abs(x - y) for x, y in zip(a, b)) < 1e-6   # padding does not change a score (last bits aside)
    assert Model.shapes == [(10, 128)]              # a full batch of 10, one of the fixed lengths


def test_slow_line_counts_scored_candidates(capsys, monkeypatch):
    search._ST.d = {"ce_scores": 2.5, "ce_n": 30.0}
    search._slow_line({"results": [1, 2]}, "xce", 2.7)
    out = capsys.readouterr().out
    assert '"ce_n": 30' in out and '"ce_n": 30000' not in out
    search._ST.d = None
