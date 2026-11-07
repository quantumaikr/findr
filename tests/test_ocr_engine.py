"""the system's own text recognizer, where findr picks one (macOS Vision, Windows OCR with Korean): it must read Korean and
English, and be quick. Synthetic pages with known text (no user files). Prints accuracy and seconds per page."""
import os
import sys
import time
from pathlib import Path

import pytest

import index

LINES = ["2025년 3분기 영업 전략 회의록", "스마트센서 게이트웨이 견적서 (수량 12대, 단가 520,000원)", "근로계약서 제3조 (근무시간) 주 40시간",
         "Project Kickoff Meeting - Q3 Roadmap Review", "서울특별시 강남구 테헤란로 152, 12층", "Invoice No. 2025-0917 Total KRW 3,450,000"]
FONTS = ["C:/Windows/Fonts/malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]


def dist(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@pytest.mark.skipif(index.ocr_engine() == "easyocr", reason="no system recognizer here (EasyOCR is covered by the end-to-end test)")
def test_system_ocr_reads_korean():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    font = next((f for f in FONTS if Path(f).exists()), None)
    if not font:
        pytest.skip("no Korean font")
    f = ImageFont.truetype(font, 40)
    err = tot = 0; secs = []
    for k in range(3):
        img = Image.new("RGB", (1654, 1100), "white"); d = ImageDraw.Draw(img)
        lines = LINES[k:] + LINES[:k]
        for i, ln in enumerate(lines):
            d.text((100, 80 + i * 150), ln, fill="black", font=f)
        t = time.time(); got = index.ocr_lines(np.array(img)); secs.append(time.time() - t)
        g, w = "".join("".join(got).split()), "".join("".join(lines).split())
        err += dist(g, w); tot += len(w)
    acc = 1 - err / tot
    print(f"\nOCR {index.ocr_engine()}: {acc:.1%} of characters, {sum(secs[1:]) / 2:.2f} s/page (first page {secs[0]:.2f} s)")
    assert acc >= 0.85, f"{index.ocr_engine()} read only {acc:.1%}"
    assert sum(secs[1:]) / 2 < 5
