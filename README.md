<p align="center"><img src="assets/logo.svg" width="96" alt="findr"></p>

<h1 align="center">findr</h1>
<p align="center"><b>기억나는 대로 물으면, 내 PC가 찾아 줍니다.</b></p>

> 비공개 시험판 0.2. 설치와 사용법은 아래를, 바뀐 점은 [CHANGELOG](CHANGELOG.md)를 보세요.

파일 이름을 몰라도 됩니다. **"지난달 받은 견적서"**, **"작년 여름 바다에서 찍은 사진"**, **"최종본 말고 그 직전 판"**처럼
기억나는 대로 물으면 내 컴퓨터에서 찾아, 맞을 가능성이 높은 순서로 보여 줍니다. 확실한 파일이 없으면 그렇다고 말합니다.

- 🔒 **모든 처리는 내 PC 안에서**: 파일 내용이 인터넷으로 나가지 않습니다
- 🗂️ **PC 전체의 내 파일만**: 시스템·프로그램 폴더는 빼고, 바탕화면·다운로드부터 색인합니다. 색인 중에도 검색할 수 있습니다
- 🖼️ **사진도 내용으로**: "고양이가 노트북 위에 누운 사진" (GPU가 있는 PC)
- 🎯 **후보마다 맞을 가능성 %**와 이유, 여러 페이지 후보, 고른 결과를 기억해 점점 나에게 맞춰짐
- 💻 **GPU가 없어도 동작**: 작은 모델로 자동 전환

## 설치

필요한 것: **git**, 인터넷(처음 설치할 때만), 디스크 10GB 정도. 파이썬은 설치 스크립트가 따로 준비합니다.

**Windows** (PowerShell)
```powershell
gh repo clone quantumaikr/findr      # 또는 git clone https://github.com/quantumaikr/findr
cd findr
powershell -ExecutionPolicy Bypass -File install.ps1
```

**macOS · Linux**
```bash
git clone https://github.com/quantumaikr/findr && cd findr
bash install.sh
```

설치 스크립트가 이 PC에 맞는 모델을 알아서 받습니다(로그인 필요 없음). 다시 받으려면 `findr setup`.

## 사용

| 명령 (Windows는 `.\findr`, macOS·Linux는 `./findr.sh`) | 하는 일 |
|---|---|
| `findr index` | PC 전체의 내 파일 색인 (처음 한 번, 이후 다시 실행하면 바뀐 것만) |
| `findr index D:\자료 E:\사진` | 원하는 폴더만 색인 |
| `findr serve` | 검색 화면 열기 → 브라우저에서 `http://127.0.0.1:8096` |
| `findr ask "지난주에 고친 보고서"` | 터미널에서 바로 찾기 |
| `findr doctor` | 설치 상태 점검 |

검색 화면은 비밀번호로 보호됩니다. `findr serve`가 처음 실행될 때 비밀번호를 만들어 화면에 알려 줍니다(사용자 이름은 아무거나).

## PC에 따라 달라지는 것

| | NVIDIA GPU (8GB 이상) | Apple 칩 Mac | GPU 없음 |
|---|---|---|---|
| 의미 검색 | 큰 모델 | 큰 모델 | 작은 모델 |
| 후보 나란히 비교 | ○ | ○ | – |
| 사진 내용 검색 | ○ (GPU 10GB 이상 권장) | – (사진은 이름·날짜로) | – (사진은 이름·날짜로) |
| 받는 모델 크기 | 약 6GB (+사진 7GB) | 약 6GB | 약 3.5GB |

## 데이터 위치

색인·설정·배운 것은 한 폴더에만 저장됩니다: Windows `%LOCALAPPDATA%\findr`, macOS·Linux `~/.findr`.
지우면 findr가 남긴 것이 모두 사라집니다. 검색한 파일 자체는 읽기만 하고 바꾸지 않습니다.
