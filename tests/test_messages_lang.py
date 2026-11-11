"""the sentences the engine sends with an answer come in Korean or English (lang=en on the request): the helper picks by
the request, Korean stays the default, whole answers are cached per language, and code that tests a sentence holds for both"""
import re
import sqlite3
import threading

import pytest

import search


class F:
    def __init__(self, con):
        self.con = con


@pytest.fixture(autouse=True)
def korean_after():
    yield
    search.set_lang("ko")


def test_helper_picks_by_language():
    assert search.lang() == "ko" and search.M("없음", "none") == "없음"          # nothing said: Korean
    search.set_lang("en")
    assert search.lang() == "en" and search.M("없음", "none") == "none"
    for other in ("ko", "", None, "fr", "EN"):                                   # anything but 'en' is Korean
        search.set_lang(other)
        assert search.M("없음", "none") == "없음"


def test_language_is_per_thread():
    search.set_lang("en")
    seen = []
    t = threading.Thread(target=lambda: seen.append(search.M("없음", "none"))); t.start(); t.join()
    assert seen == ["없음"] and search.M("없음", "none") == "none"                # another request's thread is not changed


def test_cache_key_holds_the_language(monkeypatch, tmp_path):
    db = tmp_path / "i.db"
    a = sqlite3.connect(db); a.execute("CREATE TABLE t(x)"); a.commit()
    f = F(sqlite3.connect(db))
    monkeypatch.setattr(search, "personal", lambda: None)
    ko = search.result_key(f, "회의록", "xce", 10, None, None)
    search.set_lang("en")
    en = search.result_key(f, "회의록", "xce", 10, None, None)
    assert ko != en and en == search.result_key(f, "회의록", "xce", 10, None, None)
    search.set_lang("ko")
    assert ko == search.result_key(f, "회의록", "xce", 10, None, None)


def test_softened_note_in_both_languages(monkeypatch):
    monkeypatch.setattr(search, "_CALIB", [0.0] * 9, raising=False)
    for lg, tail, hangul in (("ko", "맞는 파일을 골라 주세요.", True), ("en", "Please pick the right one.", False)):
        search.set_lang(lg)
        note = search.M("확실히 맞는 파일을 찾지 못했습니다. ", "Couldn't find a file that clearly matches. ") + search.M(*search.CLOSEST)
        r = search.add_probs({"results": [{"fid": 1, "ce": 0.9}, {"fid": 2, "ce": 0.2}], "note": note})
        assert r["note"].endswith(tail) and search.M(*search.CLOSEST) not in r["note"]    # the closing sentence was replaced
        assert bool(re.search("[가-힣]", r["note"] + " ".join(r["results"][0]["why"]))) is hangul


def test_sentence_tests_hold_in_english():
    src = open(search.__file__, encoding="utf-8").read()
    # the gate reads a "must-have word is missing" note by its opening quote: both English notes open with it too
    assert src.count("""f"'{miss[2]}' is """) == 2
    # the last answers kept for /refine are per language, like the answer cache
    assert "_LAST.get((q_, mode, lang()))" in src and src.count("_LAST[(q_, mode, lang())]") == 2
