"""(1) no developer messages on the person's screen (#67), unless FINDR_VERBOSE=1; (2) slow-question lines also land in the data
folder's logs, where the tester guide looks (#67: CLI serve printed them to the terminal only)"""
import importlib
import json

from findr import paths
import search


def test_quiet_sets_library_switches(monkeypatch):
    for k in ("HF_HUB_VERBOSITY", "TRANSFORMERS_VERBOSITY", "HF_HUB_DISABLE_IMPLICIT_TOKEN", "FINDR_VERBOSE"):
        monkeypatch.delenv(k, raising=False)
    paths.quiet()
    import os
    assert os.environ["HF_HUB_VERBOSITY"] == "error" and os.environ["TRANSFORMERS_VERBOSITY"] == "error"


def test_verbose_keeps_everything(monkeypatch):
    monkeypatch.delenv("HF_HUB_VERBOSITY", raising=False)
    monkeypatch.setenv("FINDR_VERBOSE", "1")
    paths.quiet()
    import os
    assert "HF_HUB_VERBOSITY" not in os.environ


def test_slow_line_goes_to_logs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    search._ST.d = {"ce_scores": 2.5}
    search._slow_line({"results": [1, 2]}, "xce", 3.1)
    out = capsys.readouterr().out
    assert out.startswith("FINDR_SLOW ")
    line = (tmp_path / "logs" / "slow.log").read_text(encoding="utf-8").strip()
    rec = json.loads(line.split("FINDR_SLOW ", 1)[1])
    assert rec["ms"] == 3100 and rec["stages"]["ce_scores"] == 2500 and rec["n"] == 2
