# Install the findr desktop app on Windows from the latest release.
#   irm https://raw.githubusercontent.com/quantumaikr/findr/main/get-app.ps1 | iex
# What it does: finds the latest release, downloads the installer (findr_<version>_x64-setup.exe), checks it against
# the release's SHA256SUMS.txt (stops on a mismatch), and starts it. Nothing else on the PC is changed, and no security
# setting is touched: where Smart App Control or a company policy blocks the installer, it stays blocked.
# Messages are Korean or English ($env:FINDR_LANG = ko|en, else the display language). $env:FINDR_VERSION picks a version;
# $env:FINDR_DOWNLOAD_ONLY = 1 downloads and checks the installer into the current folder without starting it.
$ErrorActionPreference = 'Stop'
$Repo = 'quantumaikr/findr'
$lang = "$env:FINDR_LANG"
if ($lang -ne 'ko' -and $lang -ne 'en') { $lang = if ((Get-UICulture).TwoLetterISOLanguageName -eq 'ko') { 'ko' } else { 'en' } }
function Say($ko, $en) { if ($lang -eq 'ko') { Write-Host $ko } else { Write-Host $en } }
function Fail($ko, $en) { Say $ko $en; throw 'findr-get-app' }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$tmp = $null
try {
  if (-not [Environment]::Is64BitOperatingSystem) { Fail '64비트 Windows가 필요합니다.' 'A 64-bit Windows is needed.' }
  $api = if ($env:FINDR_VERSION) { "https://api.github.com/repos/$Repo/releases/tags/app-v$($env:FINDR_VERSION)" } else { "https://api.github.com/repos/$Repo/releases/latest" }
  # a private repository (or a rate limit) needs a token: GITHUB_TOKEN, else the one gh is signed in with
  $token = "$env:GITHUB_TOKEN"
  if (-not $token -and (Get-Command gh -ErrorAction SilentlyContinue)) { try { $token = "$(gh auth token 2>$null)".Trim() } catch { $token = '' } }
  $h = @{ 'Accept' = 'application/vnd.github+json'; 'User-Agent' = 'findr-get-app' }
  if ($token) { $h['Authorization'] = "Bearer $token" }
  try { $rel = Invoke-RestMethod -Uri $api -Headers $h -UseBasicParsing } catch { Fail '배포 정보를 읽지 못했습니다. 인터넷 연결과 버전 이름을, 비공개 저장소라면 GITHUB_TOKEN이나 gh 로그인을 확인하세요.' 'Could not read the release. Check the internet connection and the version; for a private repository, GITHUB_TOKEN or the gh sign-in.' }
  $app = $rel.assets | Where-Object { $_.name -like '*_x64-setup.exe' } | Select-Object -First 1
  $sums = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
  if (-not $app) { Fail "이 배포($($rel.tag_name))에 Windows 설치 파일이 없습니다." "This release ($($rel.tag_name)) has no Windows installer." }

  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('findr-' + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  $dl = $h.Clone(); $dl['Accept'] = 'application/octet-stream'
  $file = Join-Path $tmp $app.name
  Say "findr $($rel.tag_name) 받는 중: $($app.name)" "Downloading findr $($rel.tag_name): $($app.name)"
  $ProgressPreference = 'SilentlyContinue'   # the progress bar makes Windows PowerShell 5.1 downloads many times slower
  Invoke-WebRequest -Uri $app.url -Headers $dl -OutFile $file -UseBasicParsing
  if ($sums) {
    $sf = Join-Path $tmp 'SHA256SUMS.txt'
    Invoke-WebRequest -Uri $sums.url -Headers $dl -OutFile $sf -UseBasicParsing
    $want = Get-Content $sf | ForEach-Object { $p = $_ -split '\s+', 2; if ($p.Count -eq 2 -and $p[1].TrimStart('*') -eq $app.name) { $p[0] } } | Select-Object -First 1
    $got = (Get-FileHash -Algorithm SHA256 -Path $file).Hash
    if (-not $want -or $want.ToLower() -ne $got.ToLower()) { Fail '검증 실패: 받은 파일이 배포된 파일과 다릅니다. 설치하지 않았습니다.' 'Check failed: the download differs from the released file. Nothing was installed.' }
    Say '검증 완료 (SHA-256 일치)' 'Verified (SHA-256 matches)'
  } else {
    Say '주의: 이 배포에는 검증 파일이 없어 확인 없이 설치합니다.' 'Note: this release has no checksums file, so it is installed unchecked.'
  }
  if ($env:FINDR_DOWNLOAD_ONLY) {   # checked, then kept in the current folder instead of installed
    $keep = Join-Path (Get-Location) $app.name
    Move-Item -Force $file $keep
    Say "받기만 했습니다: $keep" "Downloaded only: $keep"
    return
  }
  Say '설치 프로그램을 엽니다. 안내에 따라 진행하세요.' 'Opening the installer. Follow its steps.'
  try { $p = Start-Process -FilePath $file -PassThru -Wait } catch {
    Fail '설치 프로그램을 열지 못했습니다. 스마트 앱 컨트롤이나 회사 정책이 서명 없는 프로그램을 막았을 수 있습니다.' 'The installer could not start. Smart App Control or a company policy may block unsigned programs.'
  }
  if ($p.ExitCode -ne 0) { Fail "설치가 끝나지 않았습니다 (코드 $($p.ExitCode))." "The installation did not finish (code $($p.ExitCode))." }
  Say '설치했습니다. 시작 메뉴에서 findr를 여세요. 처음 실행하면 이 PC에 맞는 구성 요소와 모델을 받습니다(5~20분).' 'Installed. Open findr from the Start menu. On the first run it downloads what suits this PC (5-20 minutes).'
} catch {
  # 'irm | iex' runs in the person's own window: no 'exit' (it would close it), only the message
  if ("$_" -ne 'findr-get-app') { Say "설치하지 못했습니다: $_" "Could not install: $_" }
} finally {
  if ($tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}
