# findr 설치 (Windows). 저장소 폴더에서 PowerShell로:  powershell -ExecutionPolicy Bypass -File install.ps1
# 하는 일: uv(파이썬 관리 도구) 설치 -> 이 폴더에 전용 파이썬 환경(.venv) -> GPU에 맞는 torch -> findr -> 모델 받기
# Install findr (Windows). In the repository folder, with PowerShell:  powershell -ExecutionPolicy Bypass -File install.ps1
# What it does: uv (the Python manager) -> a Python environment of its own in this folder (.venv) -> torch for the GPU -> findr -> the models
# Messages are Korean or English: $env:FINDR_LANG = ko | en, else the Windows display language.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$lg = "$env:FINDR_LANG".Trim().ToLower()
if ($lg.StartsWith("ko")) { $ko = $true } elseif ($lg.StartsWith("en")) { $ko = $false } else { $ko = (Get-UICulture).Name -like "ko*" }
function Say([string]$Korean, [string]$English, [string]$Color = "") {
  $m = if ($ko) { $Korean } else { $English }
  if ($Color) { Write-Host $m -ForegroundColor $Color } else { Write-Host $m }
}
Say "findr 설치를 시작합니다." "Installing findr." Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Say "git이 필요합니다 (언어 모델 라이브러리를 시험한 판으로 받기 위해). 설치 후 다시 실행하세요:  winget install Git.Git" "git is needed (to get the tested version of the language model library). Install it and run this again:  winget install Git.Git" Yellow
  exit 1
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "[1/5] uv 설치" "[1/5] Installing uv"
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Say "[2/5] 전용 파이썬 환경 (.venv, Python 3.12)" "[2/5] A Python environment of its own (.venv, Python 3.12)"
uv venv .venv --python 3.12 --allow-existing
if ($LASTEXITCODE -ne 0) { Say "  파이썬 환경을 만들지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요." "  Could not create the Python environment. Check your internet connection and run this again."; exit 1 }
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

$gpu = $false
try { if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi -L | Out-Null; $gpu = ($LASTEXITCODE -eq 0) } } catch {}
if ($gpu) { $idx = "https://download.pytorch.org/whl/cu128"; Say "[3/5] NVIDIA GPU 확인: GPU용 torch" "[3/5] NVIDIA GPU found: torch for the GPU" }
else      { $idx = "https://download.pytorch.org/whl/cpu";   Say "[3/5] GPU 없음: CPU용 torch (작은 모델로 동작)" "[3/5] No GPU: torch for the CPU (runs with the small models)" }
uv pip install --python $py torch torchvision --index-url $idx
if ($LASTEXITCODE -ne 0) { Say "  PyTorch를 설치하지 못했습니다. 인터넷 연결과 디스크 여유 공간을 확인하고 다시 실행하세요." "  Could not install PyTorch. Check your internet connection and free disk space, then run this again."; exit 1 }

Write-Host "[4/5] findr"
uv pip install --python $py -e .
if ($LASTEXITCODE -ne 0) { Say "  findr를 설치하지 못했습니다. 위의 오류를 확인하고 다시 실행하세요." "  Could not install findr. See the error above, then run this again."; exit 1 }
Set-Content -Path (Join-Path $PSScriptRoot "findr.cmd") -Encoding ascii -Value "@echo off`r`n`"%~dp0.venv\Scripts\findr.exe`" %*"

Say "[5/5] 모델 받기 (처음 한 번, PC에 따라 1~10GB)" "[5/5] Downloading the models (once, 1-10GB depending on the PC)"
& (Join-Path $PSScriptRoot ".venv\Scripts\findr.exe") setup
if ($LASTEXITCODE -ne 0) {
  Say "`n모델을 받지 못했습니다. 인터넷 연결을 확인하세요. 회사 망에서 막히면 허깅페이스 로그인 후 다시. 아래를 실행한 뒤 다시:  .\findr setup" "`nCould not download the models. Check your internet connection. If a company network blocks it, log in to Hugging Face with the command below, then run:  .\findr setup" Yellow
  Write-Host "  .\.venv\Scripts\hf.exe auth login"
  exit 1
}
Say "`n설치 완료. 다음 순서로 쓰세요:" "`nInstalled. Next:" Green
Say "  .\findr index     (PC 전체의 내 파일 색인)" "  .\findr index     (index your files on the whole PC)"
Say "  .\findr serve     (검색 화면)" "  .\findr serve     (the search page)"
