# findr 설치 (Windows). 저장소 폴더에서 PowerShell로:  powershell -ExecutionPolicy Bypass -File install.ps1
# 하는 일: uv(파이썬 관리 도구) 설치 -> 이 폴더에 전용 파이썬 환경(.venv) -> GPU에 맞는 torch -> findr -> 모델 받기
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "findr 설치를 시작합니다." -ForegroundColor Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "git이 필요합니다 (언어 모델 라이브러리를 시험한 판으로 받기 위해). 설치 후 다시 실행하세요:  winget install Git.Git" -ForegroundColor Yellow
  exit 1
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "[1/5] uv 설치"
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Write-Host "[2/5] 전용 파이썬 환경 (.venv, Python 3.12)"
uv venv .venv --python 3.12 --allow-existing
if ($LASTEXITCODE -ne 0) { Write-Host "  파이썬 환경을 만들지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요."; exit 1 }
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

$gpu = $false
try { if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi -L | Out-Null; $gpu = ($LASTEXITCODE -eq 0) } } catch {}
if ($gpu) { $idx = "https://download.pytorch.org/whl/cu128"; Write-Host "[3/5] NVIDIA GPU 확인: GPU용 torch" }
else      { $idx = "https://download.pytorch.org/whl/cpu";   Write-Host "[3/5] GPU 없음: CPU용 torch (작은 모델로 동작)" }
uv pip install --python $py torch torchvision --index-url $idx
if ($LASTEXITCODE -ne 0) { Write-Host "  PyTorch를 설치하지 못했습니다. 인터넷 연결과 디스크 여유 공간을 확인하고 다시 실행하세요."; exit 1 }

Write-Host "[4/5] findr"
uv pip install --python $py -e .
if ($LASTEXITCODE -ne 0) { Write-Host "  findr를 설치하지 못했습니다. 위의 오류를 확인하고 다시 실행하세요."; exit 1 }
Set-Content -Path (Join-Path $PSScriptRoot "findr.cmd") -Encoding ascii -Value "@echo off`r`n`"%~dp0.venv\Scripts\findr.exe`" %*"

Write-Host "[5/5] 모델 받기 (처음 한 번, PC에 따라 1~10GB)"
& (Join-Path $PSScriptRoot ".venv\Scripts\findr.exe") setup
if ($LASTEXITCODE -ne 0) {
  Write-Host "`n모델을 받지 못했습니다. 인터넷 연결을 확인하세요. 회사 망에서 막히면 허깅페이스 로그인 후 다시. 아래를 실행한 뒤 다시:  .\findr setup" -ForegroundColor Yellow
  Write-Host "  .\.venv\Scripts\hf.exe auth login"
  exit 1
}
Write-Host "`n설치 완료. 다음 순서로 쓰세요:" -ForegroundColor Green
Write-Host "  .\findr index     (PC 전체의 내 파일 색인)"
Write-Host "  .\findr serve     (검색 화면)"
