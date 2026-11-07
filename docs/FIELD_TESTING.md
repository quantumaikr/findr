# 실제 PC에서 시험하기 (현장 시험)

자동 시험(`tests/`, CI)은 깨끗한 가상 PC에서 돕니다. 사람이 쓰던 PC에는 한글 이름이 NFD로 저장된 수만 개 파일, 클라우드 동기화 폴더, conda 설치 폴더, 몇 년 묵은 다운로드가 있습니다. 0.2.x의 큰 문제 대부분은 이런 PC에서만 드러났습니다(아래 "만났던 함정"). 이 문서는 그런 시험을 **안전하게, 재현 가능하게** 하는 방법입니다.

## 원칙

1. **고치기 전에 상류부터 본다.** `git fetch` 후 열린·닫힌 이슈와 `origin/main`을 확인합니다. 같은 문제를 다른 사람이 이미 다른 방식으로 고쳤을 수 있습니다.
2. **실사용 색인으로 실험하지 않는다.** 가설은 작은 폴더 + 별도 DB로 확인합니다(한 번에 1~2분). 실사용 색인(수만 파일)은 한 번 돌리는 데 수십 분~수 시간이 걸립니다.
   ```bash
   .venv/bin/python findr/core/index.py --root /tmp/fixture --db /tmp/fixture.db --no-embed
   ```
3. **긴 작업 중에는 코드와 환경을 바꾸지 않는다.** 색인이 도는 동안 `git pull`, 브랜치 전환, `pip install`을 하지 않습니다. 코드를 고쳐야 하면 `git worktree`로 다른 폴더에서 합니다.
4. **되돌릴 길을 남긴다.** 색인 형식이 바뀌는 시험 전에는 `~/.findr/index.db`를 복사해 둡니다(`PRAGMA wal_checkpoint(TRUNCATE)` 뒤에).
5. **잰 것과 추정한 것을 구분한다.** 이슈에는 잰 숫자와 재는 방법을 쓰고, 추정이면 추정이라고 씁니다.

## 업데이트 뒤 준비

`git pull`은 새 의존성을 설치하지 않습니다. 업데이트할 때마다:

```bash
uv pip install --python .venv/bin/python -e .
./findr.sh doctor
```

`doctor`의 `글자 인식`이 macOS는 `macOS 기본`, 한국어 Windows는 `Windows 기본`인지 확인합니다.

## 측정 도구

| 알고 싶은 것 | 방법 |
|---|---|
| 검색이 맞는 파일을 몇 위에 내는가, 얼마나 걸리는가 | `tools/acceptance.py` (아래) |
| 느린 순간 프로세스가 무엇을 하는가 | macOS `sample <pid> 4`, 그 밖에는 `faulthandler.dump_traceback_later`. 추측하지 말고 스택을 봅니다 |
| SQL이 표 전체를 읽는가 | `sqlite3 ~/.findr/index.db "EXPLAIN QUERY PLAN <쿼리>"`에서 `SCAN` 확인, `/usr/bin/time -p sqlite3 ...`로 시간 |
| 모델 한 단계의 속도 | 같은 입력으로 해당 함수만 따로 실행(워밍업 1회 뒤 중앙값) |
| 기기에서 큰 모델이 쓸 만한가 | `~/.findr/device_check.json`의 `pieces_per_s` |

### tools/acceptance.py

정답이 정해진 질문 세트로 검색을 잽니다. 질문에는 개인 파일 이름이 들어가므로 **저장소 밖**, 데이터 폴더에 둡니다(`~/.findr/acceptance.json`).

```json
[
  {"q": "작년에 받은 견적서", "expect": ["견적서"]},
  {"q": "QLoRA 강의 자료", "expect": ["QLoRA"]},
  {"q": "2019년 제주도 항공권 영수증", "expect": []}
]
```

`expect`는 정답 파일 이름에 들어 있는 글자들입니다(NFC/NFD 무관). 빈 목록은 "없다고 답해야 하는 질문"입니다.

```bash
./findr.sh serve --no-browser &        # 검색 화면이 떠 있어야 합니다
.venv/bin/python tools/acceptance.py --rounds 3 --slow 8
```

통과(정답이 3위 안), 1위 적중, 응답 시간 중앙값·최대를 보여 줍니다. `--slow 8`이면 8초를 넘는 질문에서 서버를 `sample`해 `acceptance_slow/`에 남깁니다(macOS).

## 만났던 함정 (0.2.x, M1 Pro 16 GB)

| 증상 | 원인 | 이슈 |
|---|---|---|
| `doctor`는 `mps`인데 색인이 CPU로 하루 이상 | 장치 선택이 cuda/cpu만 봄 | #1 |
| Apple GPU에서도 큰 모델이 초당 6~9조각 | M1에는 bfloat16 하드웨어가 없음. float32로 고쳐도 부족 → 자가 시험이 작은 모델 선택 | #1 |
| Keynote 발표 자료가 검색 안 됨 | `.key`를 암호 키 파일로 분류 | #2 |
| 검색이 `Passed CPU tensor to MPS op`로 실패 | 재정렬 모델은 CPU, 입력은 mps | #7 |
| CPU 검색이 질문당 80초 | 재정렬 모델이 CPU에서 float16(transformers 5 기본값) | #10 |
| 한글 이름 파일·폴더로 못 찾음, 맞는 파일 점수 0.02 | macOS가 이름을 NFD로 저장, 질문은 NFC | #12 |
| 1분에 한 번 검색이 25초 멈춤, 첫 검색 40~57초 | 검색 중 chunks 표 전체 읽기(5.7 GB) | #21 |
| 한글 이름 옮기기가 13분 동안 무출력, 끄면 처음부터 | 커밋 한 번, 이어하기 없음 | #23 |
| 큰 모델이 느린 기기에서도 쉬는 시간에 큰 모델로 전환 | 전환 조건이 자가 시험 결과를 안 봄 | #27 |
| 글자 인식이 조용히 꺼져 있는데 `doctor`는 ✓ | `git pull` 뒤 새 의존성 미설치, 실패를 `except: pass` | #31 |
| 바탕화면 색인이 CPU 0%로 멈춤 | iCloud "데스크탑 및 문서" 동기화가 파일 정보를 막음(findr 밖의 문제) | — |
| 색인 대상이 5.6만 → 19.4만 파일 | 홈 폴더의 conda 설치·개발 폴더 포함 → `exclude.txt`, 기본 제외 목록 | #17 |

공통점: **겉보기엔 정상이었습니다.** 오류 없이 느리거나, 조용히 건너뛰거나, `doctor`가 ✓였습니다. "에러가 없다"는 "동작한다"가 아닙니다. 잰 숫자로 확인합니다.

## 보고

이슈는 `.github/ISSUE_TEMPLATE/bug_report.md` 양식으로 씁니다. 좋은 보고는 수정을 빠르게 합니다: 환경, `doctor` 출력, 재현 절차, 잰 숫자(방법 포함), 원인 위치(파일:줄), 확인하지 못한 것.
