#!/usr/bin/env bash
# findr 설치 (macOS · Linux · WSL). 저장소 폴더에서:  bash install.sh
# 하는 일: uv(파이썬 관리 도구) -> 이 폴더에 전용 파이썬 환경(.venv) -> GPU에 맞는 torch -> findr -> 모델 받기
set -e
cd "$(dirname "$0")"
echo "findr 설치를 시작합니다."
command -v git >/dev/null || { echo "git이 필요합니다. 설치 후 다시 실행하세요."; exit 1; }
if ! command -v uv >/dev/null; then
  echo "[1/5] uv 설치"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"
fi
echo "[2/5] 전용 파이썬 환경 (.venv, Python 3.12)"
uv venv .venv --python 3.12 --allow-existing
PY=.venv/bin/python
if [ "$(uname)" = "Darwin" ]; then
  echo "[3/5] macOS: Apple GPU용 torch"; uv pip install --python $PY torch
elif command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  echo "[3/5] NVIDIA GPU 확인: GPU용 torch"; uv pip install --python $PY torch --index-url https://download.pytorch.org/whl/cu128
else
  echo "[3/5] GPU 없음: CPU용 torch (작은 모델로 동작)"; uv pip install --python $PY torch --index-url https://download.pytorch.org/whl/cpu
fi
echo "[4/5] findr"
uv pip install --python $PY -e .
printf '#!/usr/bin/env bash\nexec "$(dirname "$0")/.venv/bin/findr" "$@"\n' > findr.sh && chmod +x findr.sh
echo "[5/5] 모델 받기 (처음 한 번, PC에 따라 1~10GB)"
if ! ./findr.sh setup; then
  echo; echo "모델을 받지 못했습니다. 인터넷 연결을 확인하세요. 회사 망에서 막히면 허깅페이스 로그인 후 다시:  .venv/bin/hf auth login   그 다음  ./findr.sh setup"; exit 1
fi
echo; echo "설치 완료. 다음 순서로 쓰세요:"; echo "  ./findr.sh index     (PC 전체의 내 파일 색인)"; echo "  ./findr.sh serve     (검색 화면)"
