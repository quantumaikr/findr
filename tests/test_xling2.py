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


def test_word_search_gets_english_for_korean_and_korean_for_english(monkeypatch):
    monkeypatch.setenv("XLING2", "1")
    e = search.xl_expand("현금흐름표 결론도출근거")
    assert "cash" in e and "flows" in e and "conclusions" in e
    assert "q1" in search.xl_expand("작년 1분기 투자자 설명자료").split()
    assert search.xl_expand("ethics textbook chapter") == ""     # English questions get no Korean words
    assert "map" not in search.xl_expand("지도안 양식").split()     # 지도 is not taken from inside 지도안
