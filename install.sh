#!/usr/bin/env bash
# findr 설치 (macOS · Linux · WSL). 저장소 폴더에서:  bash install.sh
# 하는 일: uv(파이썬 관리 도구) -> 이 폴더에 전용 파이썬 환경(.venv) -> GPU에 맞는 torch -> findr -> 모델 받기
# Install findr (macOS, Linux, WSL). In the repository folder:  bash install.sh
# What it does: uv (the Python manager) -> a Python environment of its own in this folder (.venv) -> torch for the GPU -> findr -> the models
# Messages are Korean or English: FINDR_LANG=ko|en, else the system's language (LC_ALL, LC_MESSAGES, LANG).
set -e
cd "$(dirname "$0")"
case "${FINDR_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in ko*|KO*|Ko*) KO=1 ;; *) KO= ;; esac
say() { if [ -n "$KO" ]; then echo "$1"; else echo "$2"; fi; }
say "findr 설치를 시작합니다." "Installing findr."
command -v git >/dev/null || { say "git이 필요합니다. 설치 후 다시 실행하세요." "git is needed. Install it and run this again."; exit 1; }
if ! command -v uv >/dev/null; then
  say "[1/5] uv 설치" "[1/5] Installing uv"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"
fi
say "[2/5] 전용 파이썬 환경 (.venv, Python 3.12)" "[2/5] A Python environment of its own (.venv, Python 3.12)"
uv venv .venv --python 3.12 --allow-existing
PY=.venv/bin/python
if [ "$(uname)" = "Darwin" ]; then
  say "[3/5] macOS: Apple GPU용 torch" "[3/5] macOS: torch for the Apple GPU"; uv pip install --python $PY torch torchvision
elif command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  say "[3/5] NVIDIA GPU 확인: GPU용 torch" "[3/5] NVIDIA GPU found: torch for the GPU"; uv pip install --python $PY torch torchvision --index-url https://download.pytorch.org/whl/cu128
else
  say "[3/5] GPU 없음: CPU용 torch (작은 모델로 동작)" "[3/5] No GPU: torch for the CPU (runs with the small models)"; uv pip install --python $PY torch torchvision --index-url https://download.pytorch.org/whl/cpu
fi
echo "[4/5] findr"
uv pip install --python $PY -e .
printf '#!/usr/bin/env bash\nexec "$(dirname "$0")/.venv/bin/findr" "$@"\n' > findr.sh && chmod +x findr.sh
say "[5/5] 모델 받기 (처음 한 번, PC에 따라 1~10GB)" "[5/5] Downloading the models (once, 1-10GB depending on the PC)"
if ! ./findr.sh setup; then
  echo; say "모델을 받지 못했습니다. 인터넷 연결을 확인하세요. 회사 망에서 막히면 허깅페이스 로그인 후 다시:  .venv/bin/hf auth login   그 다음  ./findr.sh setup" "Could not download the models. Check your internet connection. If a company network blocks it, log in to Hugging Face and try again:  .venv/bin/hf auth login   then  ./findr.sh setup"; exit 1
fi
echo; say "설치 완료. 다음 순서로 쓰세요:" "Installed. Next:"; say "  ./findr.sh index     (PC 전체의 내 파일 색인)" "  ./findr.sh index     (index your files on the whole PC)"; say "  ./findr.sh serve     (검색 화면)" "  ./findr.sh serve     (the search page)"
