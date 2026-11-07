<p align="center">
  <img src="assets/logo.svg" width="88" alt="findr">
</p>

<h1 align="center">findr</h1>

<p align="center">
  <b>기억나는 대로 물으면, 내 PC가 찾아 줍니다.</b><br>
  내 컴퓨터 안에서만 동작하는 자연어 파일·사진 검색
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.2.0-2563eb">
  <img alt="status" src="https://img.shields.io/badge/status-private%20preview-f59e0b">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-475569">
  <img alt="privacy" src="https://img.shields.io/badge/data-stays%20on%20your%20PC-16a34a">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-64748b">
</p>

<p align="center">
  <b>한국어</b> · <a href="README.en.md">English</a>
</p>

---

## 소개

파일을 찾을 때 우리는 이름보다 **기억**을 떠올립니다. "지난달 거래처에서 받은 견적서", "작년 여름 바다에서 찍은 사진", "최종본 말고 그 직전 판".
findr는 이런 말을 그대로 알아듣고 내 PC에서 파일을 찾아, **맞을 가능성이 높은 순서로** 보여 줍니다.
확실한 파일이 없으면 엉뚱한 파일을 내밀지 않고 **그렇다고 말합니다.**

```text
(예시)
> 작년 여름 휴가 때 찍은 바다 사진
  1. 92%  IMG_5082.jpg     2025-08-12   사진 내용이 질문과 맞음
  2. 71%  IMG_5090.jpg     2025-08-13   사진 내용이 질문과 맞음
  3. 38%  KakaoTalk_...jpg 2025-08-20   사진 내용이 일부 맞음
```

## 주요 기능

| | |
|---|---|
| 🗣️ **기억나는 대로 묻기** | 내용·때·사람·한 일을 함께 이해합니다. "작년 8월", "겨울방학 때", "회의 전에 고친", "김 부장이 보낸" |
| 🖼️ **사진도 내용으로** | "노트북 위에 누운 고양이", "케이크 들고 있는 두 사람". 찍은 날짜로 좁혀 찾습니다 |
| 🎯 **확률과 이유** | 후보마다 맞을 가능성(%)과 근거를 보여 주고, 여러 페이지에 걸쳐 후보를 남겨 둡니다. 최종 선택은 사람이 합니다 |
| 🧠 **나를 알아가는 검색** | 고른 파일과 "✘ 아님"을 기억해 점점 나에게 맞춰집니다. 배운 내용은 직접 보고 지울 수 있습니다 |
| 🙅 **없으면 없다고** | 기억한 조건에 맞는 파일이 없으면 솔직하게 알리고, 가장 가까운 후보를 함께 보여 줍니다 |
| 🗂️ **PC 전체, 내 파일만** | 시스템·프로그램·캐시 폴더는 건너뛰고 사람이 만들거나 받은 파일만 색인합니다. 바탕화면·다운로드부터 시작해 몇 분 안에 검색할 수 있습니다 |
| 🔒 **모든 처리는 내 PC에서** | 파일 내용과 질문이 인터넷으로 나가지 않습니다. 검색 화면은 비밀번호로 보호됩니다 |
| 💻 **GPU가 없어도** | PC 사양에 맞는 구성을 자동으로 고릅니다 |

지원 형식: PDF · HWP · HWPX · DOC · DOCX · XLSX · PPTX · TXT · Markdown · 소스 코드 · 사진(JPG · PNG · WEBP 등). 그 밖의 파일은 이름·폴더·날짜로 찾습니다.

## 성능

| 측정 | 결과 |
|---|---|
| 처음 보는 가상 PC 6대, 질문 288개 (활동 기록이 없는 일반 PC 조건) | **1순위 76%**, 3위 안 90% |
| 실제 개인 PC, 처음 보는 질문 45개 | **1순위 84%** |
| 사진 검색 (처음 보는 사진 240장 + 문서가 섞인 PC) | **1순위 85%**, 사진을 켜도 문서 검색 결과는 그대로 |
| 응답 시간 (GPU PC) | 중간값 약 0.3초 |
| 첫 색인 (문서 2,100개 PC, GPU) | 이름 검색 약 1분, 내용 검색 약 2분 30초 |

> 수치는 개발 중 측정값이며 PC 구성과 파일에 따라 달라집니다. 결과 조정에 쓰지 않은 질문으로만 측정했습니다.

## 설치

**필요한 것:** git, 인터넷(처음 설치할 때만), 여유 디스크 약 10GB. 파이썬은 설치 스크립트가 알아서 준비합니다.

<details open>
<summary><b>Windows</b></summary>

```powershell
git clone https://github.com/quantumaikr/findr
cd findr
powershell -ExecutionPolicy Bypass -File install.ps1
```
</details>

<details>
<summary><b>macOS · Linux</b></summary>

```bash
git clone https://github.com/quantumaikr/findr && cd findr
bash install.sh
```
</details>

설치 스크립트는 전용 파이썬 환경을 만들고, PC의 GPU를 확인해 맞는 구성과 모델을 받습니다. 별도 로그인은 필요 없습니다.

## 사용법

```bash
findr index          # PC 전체의 내 파일 색인 (처음 한 번, 다시 실행하면 바뀐 파일만)
findr serve          # 검색 화면 → http://127.0.0.1:8096
```

| 명령 | 설명 |
|---|---|
| `findr index` | PC 전체의 내 파일 색인. 진행 중에도 검색할 수 있습니다 |
| `findr index D:\자료 E:\사진` | 원하는 폴더만 색인 |
| `findr serve` | 검색 화면 열기. 처음 실행할 때 비밀번호를 만들어 알려 줍니다 |
| `findr ask "지난주에 고친 보고서"` | 터미널에서 바로 찾기 |
| `findr setup` | 모델 다시 받기 · 구성 바꾸기 (`--lite`, `--photo`) |
| `findr doctor` | 설치·색인 상태 점검 |

> Windows에서는 저장소 폴더의 `.\findr`, macOS·Linux에서는 `./findr.sh`로 실행합니다.

### 이렇게 물어보세요

```text
지난달 받은 견적서                    최종본 말고 그 직전 판
작년 겨울에 찍은 눈사람 사진            회의 전에 고친 발표 자료
소각로 48톤이라고 적힌 매뉴얼           카톡으로 받은 계약서 스캔
```

## PC 사양별 구성

| | NVIDIA GPU | Apple 칩 Mac | GPU 없음 |
|---|:---:|:---:|:---:|
| 내용(의미) 검색 | ● 고정밀 | ● 고정밀 | ● 경량 |
| 후보 나란히 비교 | ● | ● | – |
| 사진 내용 검색 | ● (GPU 메모리 10GB 이상) | 이름·날짜 | 이름·날짜 |
| 받는 모델 크기 | 약 5GB (+ 사진 7GB) | 약 5GB | 약 2GB |

## 개인정보와 데이터

- 파일 내용, 질문, 검색 기록은 **PC 밖으로 나가지 않습니다.** 인터넷은 처음 설치할 때 모델을 받을 때만 씁니다.
- findr가 만드는 것은 한 폴더에만 저장됩니다: Windows `%LOCALAPPDATA%\findr`, macOS·Linux `~/.findr`. 지우면 모두 사라집니다.
- 검색하는 파일은 **읽기만 하고 바꾸지 않습니다.** 비밀번호·인증서 같은 민감한 파일은 색인하지 않습니다.
- 여러 사람이 쓰는 PC에서는 **현재 사용자 폴더만** 색인합니다.

## 알려진 한계

- "누가 보냈는지", "언제 인쇄했는지" 같은 이력은 PC에 기록이 있을 때만 확인할 수 있습니다. 기록이 없으면 내용으로 찾고 그 사실을 알려 줍니다.
- 사진이 수천 장이면 "도넛 사진"처럼 넓은 질문에 맞는 사진이 여러 장일 수 있습니다. 때("작년 봄")를 함께 말하면 더 정확합니다.
- GPU가 없는 PC에서는 정밀 비교 단계가 느려 응답이 1~2초 걸릴 수 있습니다.

## 라이선스

Apache License 2.0. 함께 받는 모델은 각 배포처의 라이선스를 따릅니다.
보안 문제는 [SECURITY.md](SECURITY.md)를 참고해 비공개로 알려 주세요. 바뀐 점은 [CHANGELOG](CHANGELOG.md)에 있습니다.
