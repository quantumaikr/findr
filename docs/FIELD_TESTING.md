# 현장 시험 절차

실제 PC에서 findr를 시험하는 **순서와 도구**입니다. 왜 이렇게 하는지(원칙)는 [LESSONS.md](LESSONS.md)에 있고, 여기서는 그 번호를 `L7`처럼 가리킵니다.

## 1. 준비

```bash
git -C findr pull                                  # 실행 중인 폴더라면 먼저 색인·검색을 멈춘다 (L10)
uv pip install --python .venv/bin/python -e .      # git pull은 새 의존성을 설치하지 않는다 (L12)
./findr.sh doctor
```

`doctor`에서 장치(`cuda` / `mps` / `cpu`), `글자 인식`(macOS는 `macOS 기본`, 한국어 Windows는 `Windows 기본`), 색인 규모를 확인합니다. Apple GPU라면 `~/.findr/device_check.json`의 `pieces_per_s`로 큰 모델이 이 기기에서 쓸 만한지 봅니다(L1).

## 2. 작은 폴더로 먼저

가설은 실제 색인이 아니라 작은 폴더와 별도 DB로 확인합니다. 한 번에 1~2분이면 됩니다.

```bash
.venv/bin/python findr/core/index.py --root /tmp/fixture --db /tmp/fixture.db --no-embed
```

- 한글 이름은 NFD로도 만들어 봅니다: `unicodedata.normalize("NFD", "회의록.txt")` (L7)
- 옛 형식 색인이 필요하면 그 시점 커밋을 `git worktree`로 꺼내 그 코드로 색인합니다. 업그레이드 경로는 이렇게 시험합니다(L3).
- 중간에 끊겨도 이어지는지 볼 때는 첫 진행 표시가 나오면 `kill -9` 하고 다시 실행합니다(L4).

## 3. 실제 PC에서

- 코드를 고치거나 다른 브랜치를 볼 때는 `git worktree`를 씁니다. 색인이나 검색이 돌고 있는 폴더는 건드리지 않습니다(L10).
- 색인 형식이 바뀌는 시험 전에는 DB를 복사해 둡니다: `sqlite3 ~/.findr/index.db "PRAGMA wal_checkpoint(TRUNCATE)"` 뒤 `cp`.
- 색인, 평가, 검색 서버는 한 번에 하나씩 돌립니다(L16). 오래 걸리는 색인은 `caffeinate -i ./findr.sh index`로 잠자기를 막습니다(macOS).
- PC 전체 색인 범위에서 뺄 폴더는 `~/.findr/exclude.txt`에 한 줄씩 적습니다(#17).
- **새 설치를 시험할 때는 데이터 폴더를 따로 둡니다.** findr가 이미 있는 PC에서는 새로 clone해도 `install.sh`가 기본 `~/.findr`의 모델과 색인을 그대로 씁니다. 그러면 "처음 설치"와 "첫 색인"을 잴 수 없습니다. `install.sh`와 모든 명령에 `FINDR_HOME=<새 폴더>`를 붙이고, `exclude.txt`도 새 폴더에 둡니다(#67).
- 메모리를 재기 전에, 예전 실행이 남긴 `multiprocessing` 도우미(`resource_tracker`, `spawn_main`, 부모 PID 1)를 정리합니다.
- 백그라운드로 띄운 `serve`는 SIGINT를 무시할 수 있습니다(비대화형 셸). 이때는 SIGTERM으로 끕니다.

## 4. 재기

| 알고 싶은 것 | 방법 |
|---|---|
| 맞는 파일이 몇 위인가, 얼마나 걸리는가 | `tools/acceptance.py` (아래) |
| 느린 순간 프로세스가 무엇을 하는가 | macOS `sample <pid> 4`, 그 밖에는 `faulthandler.dump_traceback_later`. 추측하기 전에 스택을 본다 |
| SQL이 표 전체를 읽는가 | `sqlite3 <db> "EXPLAIN QUERY PLAN <쿼리>"`에서 `SCAN`, `/usr/bin/time -p sqlite3 …`로 시간 (L15) |
| 모델 한 단계의 속도 | 같은 입력으로 그 함수만, 워밍업 1회 뒤 중앙값 (L5) |
| 단계별로 언제 무엇이 되는가 | `FINDR_STAGE` 출력과 `progress.json`의 시각 (L13) |

### tools/acceptance.py

정답을 아는 질문 묶음으로 검색을 잽니다(L6, L15). 질문에는 개인 파일 이름이 들어가므로 저장소가 아니라 데이터 폴더에 둡니다(L9): `~/.findr/acceptance.json`

```json
[
  {"q": "작년에 받은 견적서", "expect": ["견적서"]},
  {"q": "QLoRA 강의 자료", "expect": ["QLoRA"]},
  {"q": "2019년 제주도 항공권 영수증", "expect": []}
]
```

`expect`는 정답 파일 이름에 들어 있는 글자들입니다(NFC/NFD 무관). 빈 목록은 "없다고 답해야 하는 질문"입니다.

```bash
./findr.sh serve --no-browser &
.venv/bin/python tools/acceptance.py --rounds 3 --slow 8
```

통과(정답이 3위 안), 1위 적중, 응답 시간 중앙값·최대를 보여 주고 `acceptance_result.json`에 남깁니다. `--slow 8`이면 8초를 넘는 질문마다 서버를 `sample`해 `acceptance_slow/`에 둡니다(macOS). 순위를 바꾸는 PR은 전후 결과를 비교해 1위가 바뀐 질문을 적습니다(L6).

- **비교는 같은 채점기로 합니다.** 직접 만든 스크립트는 경로로 맞추거나 "없다고 답해야 하는 질문"을 틀린 것으로 세기 쉽습니다. 실제로 15/16을 14/16으로 세어, 없던 퇴행을 의심한 적이 있습니다(#67). 기준은 `tools/acceptance.py`입니다.
- **퇴행이 의심되면 옛 코드로 먼저 확인합니다.** 같은 색인을 기준 커밋의 `git worktree`로 띄워 같은 질문을 하면 몇 분이면 됩니다.
- **응답 시간은 검색 화면(API)으로 잽니다.** `findr ask`는 실행할 때마다 모델과 색인을 새로 불러와 18~29초가 걸립니다. CPU에서는 순위도 화면과 다를 수 있습니다(#66).
- `FINDR_SLOW` 줄은 CLI `serve`에서는 터미널에만 나옵니다. 출력을 파일로 남겨 두세요: `./findr.sh serve 2>&1 | tee <데이터 폴더>/logs/serve.log`

## 5. 증상으로 찾기

| 증상 | 원인 | 원칙 | 이슈 |
|---|---|---|---|
| `doctor`는 `mps`인데 색인이 CPU로 아주 느림 | 장치 선택이 cuda/cpu만 봄 | L1 | #1 |
| Apple GPU에서 큰 모델이 초당 6~9조각 | M1에 bfloat16 하드웨어가 없음. float32로도 부족 → 작은 모델 | L1 | #1 |
| Keynote 발표 자료가 내용으로 안 찾힘 | `.key`를 암호 키로 분류 | — | #2 |
| 검색이 `Passed CPU tensor to MPS op` | 재순위 모델은 CPU, 입력은 mps | L1 | #7 |
| CPU 검색이 질문당 수십 초 | 재순위 모델이 CPU에서 float16 | L1 | #10, #13 |
| 한글 이름·폴더로 못 찾음 | macOS NFD 이름, NFC 질문 | L7 | #12 |
| 1분에 한 번 검색이 25초 멈춤, 첫 검색 40초 이상 | 검색 중 chunks 표 전체 읽기 | L15 | #21 |
| 한글 이름 옮기기가 출력 없이 오래, 끄면 처음부터 | 커밋 한 번, 이어하기 없음 | L4 | #23 |
| 큰 모델이 느린 기기에서 쉬는 시간에 큰 모델로 바뀜 | 전환 조건이 자가 시험을 안 봄 | L1 | #27 |
| 글자 인식·옛 엑셀 읽기가 조용히 꺼짐, `doctor`는 ✓ | `git pull` 뒤 새 의존성 미설치 | L12, L2 | #31 |
| 새 형식을 추가했는데 이미 색인된 파일은 그대로 | 다시 읽기 조건 없음 | L3 | #34 |
| 바탕화면 색인이 CPU 0%로 멈춤 (macOS) | iCloud "데스크탑 및 문서" 동기화가 파일 정보를 막음. findr 밖의 문제 | — | — |
| 색인 대상이 갑자기 몇 배로 | 홈 폴더의 conda·개발 폴더 포함 → `exclude.txt`, 기본 제외 목록 | — | #17 |
| `--deep`이 첫 색인이 말한 남은 조각보다 몇 배 오래 걸림 | 첫 색인에서 읽지 않은 긴 문서 뒷부분이 남은 수에 안 들어감 | L13 | #67 |
| 서버를 켠 뒤 첫 질문만 약 3초 더 걸림 (`ce_scores`) | 미리 준비(#50)가 실제 크기의 재순위 묶음을 데우지 않음 (추정) | — | #67 |
| 색인 중 가끔 질문이 15초, `FINDR_SLOW`의 `refresh`가 8~11초 | 새 벡터가 저장되면 다음 질문이 전체를 다시 불러옴 | L15 | #67 |
| Apple GPU에서 `doctor`가 쓰지 않는 큰 모델을 ✓로 표시 | `models.plan()`이 자가 시험(`big_ok`)을 안 봄 | L1 | #67 |

## 6. 보고

- 문제는 [이슈 양식](../.github/ISSUE_TEMPLATE/bug_report.md)으로: 환경, `doctor` 출력, 재현, 잰 것(방법 포함)과 추정 구분, 확인 못 한 것.
- 고친 것은 [PR 점검표](../.github/pull_request_template.md)로. 고친 버그는 `tests/test_field_regressions.py`처럼 **고치기 전 코드에서 실패하는** 시험으로 남깁니다.
- 실제 파일·회사·사람 이름은 어디에도 그대로 쓰지 않습니다(L9).
