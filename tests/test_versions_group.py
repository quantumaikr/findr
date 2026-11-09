"""versions of one document are one line, the others under it (VERSION_GROUP); a series of dated documents is not"""
import sqlite3

import search

BODY = "2027년 지역 상생 사업 제안서 1. 사업 개요 본 사업은 지역 소상공인의 온라인 판로를 넓히기 위해 " * 3


def _con(rows):
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);")
    for i, path, text in rows:
        name = path.rsplit("/", 1)[1]
        con.execute("INSERT INTO files VALUES(?,?,?,?,100,0,1)", (i, path, name, "." + name.rsplit(".", 1)[1]))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?,NULL)", (i, i, text))
    return con


def _res(rows, dates):
    return {"results": [{"fid": i, "path": p, "date": d} for (i, p, _), d in zip(rows, dates)]}


def test_versions_in_a_working_and_a_submitted_folder_are_one_line():
    rows = [(1, "/h/사업/작업/제안서_v2.hwp", BODY + "초안 검토 중"),
            (2, "/h/사업/기타/회의 메모.docx", "다음 주 일정과 담당자 정리 회의 참석자 명단 안건 세 가지와 결정 사항 그리고 후속 조치를 적어 둔다 " * 2),
            (3, "/h/사업/제출/제안서_최종.hwp", BODY + "최종 제출본")]
    out = search.group_versions(_con(rows), _res(rows, ["2026-10-01 10:00", "2026-10-02 10:00", "2026-10-09 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1, 2]          # v2 keeps its place; the final is under it, not a line
    assert out["results"][0]["versions"][0]["path"].endswith("제안서_최종.hwp") and out["versions_folded"] == 1


def test_weekly_minutes_are_a_series_not_versions():
    t = "주간 회의록 일시 장소 참석자 안건 "
    rows = [(1, "/h/팀/회의록/주간회의록_20261001.hwp", t + "1. 예산 집행 현황 점검 2. 하반기 채용 계획 논의 3. 워크숍 장소 확정 " * 2),
            (2, "/h/팀/회의록/주간회의록_20261008.hwp", t + "1. 신규 고객 제안 준비 2. 보안 점검 결과 공유 3. 다음 분기 목표 설정 " * 2)]
    out = search.group_versions(_con(rows), _res(rows, ["2026-10-01 10:00", "2026-10-08 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1, 2] and "versions" not in out["results"][0]


def test_a_hwp_and_the_pdf_made_from_it_are_one_document():
    rows = [(1, "/h/a/보고서.pdf", BODY + "결과 보고"), (2, "/h/b/c/report_upload.hwp", BODY + "결과 보고")]
    out = search.group_versions(_con(rows), _res(rows, ["2026-10-05 10:00", "2026-10-04 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1] and out["results"][0]["versions"][0]["path"].endswith(".hwp")


def test_unrelated_documents_stay_as_they_are():
    rows = [(1, "/h/a/견적서.xlsx", "견적서 품목 수량 단가 금액 합계 부가세 별도 납기 계약 조건 " * 3),
            (2, "/h/a/계약서.hwp", "용역 계약서 제1조 목적 갑과 을은 다음과 같이 계약을 체결한다 " * 3)]
    res = _res(rows, ["2026-10-05 10:00", "2026-10-04 10:00"])
    assert search.group_versions(_con(rows), res) is res


def test_the_same_form_for_another_year_is_not_a_version():
    """M1 Pro (11/9): the same application form, another year or applicant, folded as versions 7 times in 20"""
    form = "참가 신청서 성명 소속 연락처 신청 분야 활동 계획 서약 위 내용이 사실임을 확인합니다 "
    rows = [(1, "/h/신청/2025/참가신청서_2025.hwp", form + "홍길동 기획팀 데이터 분석 교육 참여 " * 4),
            (2, "/h/신청/2026/참가신청서_2026.hwp", form + "김영희 영업팀 고객 관리 실습 참여 " * 4)]
    out = search.group_versions(_con(rows), _res(rows, ["2025-03-01 10:00", "2026-03-01 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1, 2]


def test_a_standard_opening_does_not_make_documents_one():
    """a long copyright paragraph at the top of many different papers chained them into one family"""
    cp = "이 문서의 저작권은 발행 기관에 있으며 허가 없이 복제하거나 배포할 수 없습니다 모든 권리 보유 " * 4
    rows = [(1, "/h/a/기준서_수익인식.pdf", cp + "수익 인식 기준 계약 식별 수행 의무 거래 가격 배분 " * 6),
            (2, "/h/a/기준서_리스.pdf", cp + "리스 기준 사용권 자산 리스 부채 측정 할인율 적용 " * 6)]
    out = search.group_versions(_con(rows), _res(rows, ["2026-01-01 10:00", "2026-01-02 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1, 2]


def test_a_pdf_with_broken_spacing_is_still_the_same_document():
    """Windows (11/9): an edited .hwp and the signed .pdf made from it, names unlike, the PDF text with odd spaces"""
    body = BODY + "참가 신청 내용 확인 서명"
    rows = [(1, "/h/a/해오름_참가신청서.hwp", body), (2, "/h/a/해오름팀_사인포함.pdf", " ".join(body.replace(" ", "")))]
    out = search.group_versions(_con(rows), _res(rows, ["2026-10-05 10:00", "2026-10-06 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1]


def test_numbers_in_names_keep_documents_apart():
    """M1 Pro (11/9): standards sharing a copyright opening, and a March and an April menu, were folded as versions"""
    cp = "본 기준서의 저작권은 회계기준원에 있습니다 무단 전재와 복제를 금합니다 " * 8
    rows = [(1, "/h/a/기준서 제1115호 수익.pdf", cp + "수익"), (2, "/h/a/기준서 제1116호 리스.pdf", cp + "리스"),
            (3, "/h/b/식단표_3월.hwp", "학교 급식 식단표 월 화 수 목 금 " * 12), (4, "/h/b/식단표_4월.hwp", "학교 급식 식단표 월 화 수 목 금 " * 12)]
    out = search.group_versions(_con(rows), _res(rows, ["2026-01-01 10:00"] * 4))
    assert [x["fid"] for x in out["results"]] == [1, 2, 3, 4]


def test_the_same_form_of_another_school_is_not_a_version():
    form = "2023 진로 체험 신청서 학교명 담당 교사 연락처 참가 인원 희망 분야 위와 같이 신청합니다 " * 3
    rows = [(1, "/h/c/가람중_2023_인쇄용.hwp", form + "가람중학교 김민수 010 1234 30명 과학"),
            (2, "/h/c/누리중_2023_인쇄용.hwp", form + "누리중학교 박지현 010 5678 25명 예술")]
    out = search.group_versions(_con(rows), _res(rows, ["2023-05-01 10:00", "2023-05-02 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1, 2]


def test_drafts_numbered_in_their_mark_are_versions():
    """M1 Pro (11/9): '(1작성중)' and '(2작성중1012)' mark versions of one document; their numbers are not ids"""
    rows = [(1, "/h/d/(1작성중)연구계획서.hwp", BODY + "초안"), (2, "/h/d/(2작성중1012)연구계획서.hwp", BODY + "보완")]
    out = search.group_versions(_con(rows), _res(rows, ["2026-10-01 10:00", "2026-10-12 10:00"]))
    assert [x["fid"] for x in out["results"]] == [1]
