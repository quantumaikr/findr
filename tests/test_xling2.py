"""numbered parts and the extra word list for Korean questions about English names (XLING2=1)"""
import search


def test_numbered_parts_match_english_names(monkeypatch):
    monkeypatch.setenv("XLING2", "1")
    ideas = search.xling_ideas("통계 3강 자막")
    assert any("lec03" in i and "lecture3" in i for i in ideas)
    hit = lambda name: search.xling_cover(name, ideas)[0]
    assert hit("Stats_lec03_subtitle.srt") or hit("stats lecture3 captions.json")
    assert not hit("Stats_lec04_subtitle.srt")          # another lecture


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("XLING2", raising=False)
    assert not any("lec03" in i for i in search.xling_ideas("통계 3강 자막"))
