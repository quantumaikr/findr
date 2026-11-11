"""on a CPU the number of candidates the reranker scores follows the PC's measured speed, never the clock of a question"""
import json

import search


def test_count_is_set_by_measured_speed_and_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.setattr(search, "NCAND", 5)
    search._CPU_N.clear(); monkeypatch.setitem(search.CE_RATE, "fast", 0.12); monkeypatch.setitem(search.CE_RATE, "slow", 0.66)
    assert search.cpu_candidates("fast") == 5            # five fit 1.1 s
    search._CPU_N.clear(); (tmp_path / "rerank_cpu.json").unlink()
    assert search.cpu_candidates("slow") == 3            # a 2019 laptop: three
    assert json.loads((tmp_path / "rerank_cpu.json").read_text())["n"] == 3


def test_a_restart_with_a_slightly_different_measure_keeps_the_count(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.setattr(search, "NCAND", 5)
    (tmp_path / "rerank_cpu.json").write_text(json.dumps({"n": 5, "rate": 0.21, "ncand": 5}))
    search._CPU_N.clear(); monkeypatch.setitem(search.CE_RATE, "d", 0.24)   # 0.24 x 5 = 1.2 s would say 3: the stored 5 stays
    assert search.cpu_candidates("d") == 5
    search._CPU_N.clear(); monkeypatch.setitem(search.CE_RATE, "d", 0.50)   # really slower now (over a third): decided again
    assert search.cpu_candidates("d") == 3
