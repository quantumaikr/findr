"""which photos are read for text on a first pass: screenshots always; with the system recognizer also the newest photos
(Mac Air 11/9: half of random photos in the person's folders held text, 1 of 1,443 had any in the index)"""
import index


def test_first_pass_reads_the_newest_photos_with_a_system_recognizer(monkeypatch):
    rows = [(i, f"/d/photo_{i}.jpg") for i in range(500)] + [(900, "/d/Screenshot 2026-11-09.png")]   # newest first
    monkeypatch.setenv("FINDR_OCR_PHOTOS", "300")
    got = index.photos_to_read(rows, deep=False, engine="vision")
    assert len(got) == 301 and got[0][0] == 900 and got[1][0] == 0       # the screenshot, then the 300 newest
    assert [r[0] for r in index.photos_to_read(rows, deep=False, engine="easyocr")] == [900]   # slow recognizer: as before
    assert len(index.photos_to_read(rows, deep=True, engine="easyocr")) == 501                 # a deep run: all
    monkeypatch.setenv("FINDR_OCR_PHOTOS", "0")
    assert [r[0] for r in index.photos_to_read(rows, deep=False, engine="windows")] == [900]   # 0: off
