#!/bin/sh
# Install the findr desktop app from the latest release (macOS on Apple silicon, Linux).
#   curl -fsSL https://raw.githubusercontent.com/quantumaikr/findr/main/get-app.sh | sh
# What it does: finds the latest release, downloads the app for this system, checks it against the release's
# SHA256SUMS.txt (stops on a mismatch), and installs it:
#   macOS  findr.app into /Applications (or ~/Applications when /Applications is not writable)
#   Linux  the AppImage into ~/.local/bin/findr-app (no root needed)
# Nothing else on the PC is changed, and no security setting is touched. Messages are Korean or English (FINDR_LANG=ko|en,
# else the system's language). FINDR_VERSION=0.2.21 installs that version instead of the latest; FINDR_DOWNLOAD_ONLY=1
# downloads and checks the file into the current folder without installing it.
set -eu
REPO=quantumaikr/findr
case "${FINDR_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in ko*|KO*|Ko*) KO=1 ;; *) KO= ;; esac
say() { if [ -n "$KO" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }
die() { say "$1" "$2" >&2; exit 1; }
command -v curl >/dev/null || die "curl이 필요합니다." "curl is needed."

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) WANT='_aarch64[.]app[.]tar[.]gz$' ;;
  Darwin-*) die "이 Mac은 Intel 칩입니다. findr 앱은 Apple 칩 Mac용만 있습니다. 명령줄 설치(install.sh)를 쓰세요." \
                "This Mac has an Intel chip; the findr app is built for Apple silicon only. Use the command-line install (install.sh)." ;;
  Linux-x86_64) WANT='_amd64[.]AppImage$' ;;
  *) die "이 시스템용 앱이 없습니다: $(uname -s) $(uname -m)" "No app for this system: $(uname -s) $(uname -m)" ;;
esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if [ -n "${FINDR_VERSION:-}" ]; then
  API="https://api.github.com/repos/$REPO/releases/tags/app-v$FINDR_VERSION"
else
  API="https://api.github.com/repos/$REPO/releases/latest"
fi
# a private repository (or a rate limit) needs a token: GITHUB_TOKEN, else the one gh is signed in with
TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ] && command -v gh >/dev/null 2>&1; then TOKEN=$(gh auth token 2>/dev/null || true); fi
get() {   # get URL FILE [accept]
  if [ -n "$TOKEN" ]; then
    curl -fsSL -H "Authorization: Bearer $TOKEN" -H "Accept: ${3:-application/vnd.github+json}" -o "$2" "$1"
  else
    curl -fsSL -H "Accept: ${3:-application/vnd.github+json}" -o "$2" "$1"
  fi
}
get "$API" "$TMP/release.json" || die "배포 정보를 읽지 못했습니다. 인터넷 연결과 버전 이름을, 비공개 저장소라면 GITHUB_TOKEN이나 gh 로그인을 확인하세요." "Could not read the release. Check the internet connection and the version; for a private repository, GITHUB_TOKEN or the gh sign-in."
TAG=$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$TMP/release.json" | head -1)
# each asset as "API-address name" (the API address works for private repositories too)
pick() {
  tr ',' '\n' < "$TMP/release.json" \
    | sed -n -e 's/.*"url": *"\(https:[^"]*\/releases\/assets\/[0-9]*\)".*/U \1/p' -e 's/.*"name": *"\([^"]*\)".*/N \1/p' \
    | awk -v pat="$1" '$1=="U"{u=$2; next} $1=="N" && u!="" && $2 ~ pat {print u" "$2; exit} $1=="N"{u=""}'
}
APP=$(pick "$WANT")
SUMS=$(pick '^SHA256SUMS[.]txt$')
[ -n "$APP" ] || die "이 배포($TAG)에 이 시스템용 파일이 없습니다." "This release ($TAG) has no file for this system."
NAME=${APP#* }
say "findr $TAG 받는 중: $NAME" "Downloading findr $TAG: $NAME"
get "${APP%% *}" "$TMP/$NAME" application/octet-stream || die "내려받기에 실패했습니다." "The download failed."

if [ -n "$SUMS" ]; then
  get "${SUMS%% *}" "$TMP/SHA256SUMS.txt" application/octet-stream || die "검증 파일을 받지 못했습니다." "Could not download the checksums."
  WANTSUM=$(awk -v n="$NAME" '$2==n || $2=="*"n {print $1}' "$TMP/SHA256SUMS.txt")
  if command -v sha256sum >/dev/null; then GOT=$(sha256sum "$TMP/$NAME" | cut -d' ' -f1); else GOT=$(shasum -a 256 "$TMP/$NAME" | cut -d' ' -f1); fi
  if [ -z "$WANTSUM" ] || [ "$WANTSUM" != "$GOT" ]; then
    die "검증 실패: 받은 파일이 배포된 파일과 다릅니다. 설치하지 않았습니다." "Check failed: the download differs from the released file. Nothing was installed."
  fi
  say "검증 완료 (SHA-256 일치)" "Verified (SHA-256 matches)"
else
  say "주의: 이 배포에는 검증 파일이 없어 확인 없이 설치합니다." "Note: this release has no checksums file, so it is installed unchecked."
fi

if [ -n "${FINDR_DOWNLOAD_ONLY:-}" ]; then   # checked, then kept here instead of installed
  mv "$TMP/$NAME" "./$NAME"
  say "받기만 했습니다: ./$NAME" "Downloaded only: ./$NAME"
  exit 0
fi
case "$(uname -s)" in
  Darwin)
    DEST=/Applications
    if [ ! -w "$DEST" ]; then DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
    if pgrep -x findr >/dev/null 2>&1; then die "findr가 켜져 있습니다. 끝낸 뒤 다시 실행하세요." "findr is running. Quit it and run this again."; fi
    tar -xzf "$TMP/$NAME" -C "$TMP"
    [ -d "$TMP/findr.app" ] || die "받은 파일에 findr.app이 없습니다." "The download has no findr.app."
    rm -rf "$DEST/findr.app"
    mv "$TMP/findr.app" "$DEST/findr.app"
    say "설치했습니다: $DEST/findr.app  (실행: open \"$DEST/findr.app\")" "Installed: $DEST/findr.app  (run: open \"$DEST/findr.app\")"
    ;;
  Linux)
    DEST="$HOME/.local/bin"
    mkdir -p "$DEST"
    mv "$TMP/$NAME" "$DEST/findr-app"
    chmod +x "$DEST/findr-app"
    say "설치했습니다: $DEST/findr-app  (Ubuntu 22.04 이상, libfuse2가 필요할 수 있음)" "Installed: $DEST/findr-app  (Ubuntu 22.04 or later; libfuse2 may be needed)"
    ;;
esac
say "처음 실행하면 이 PC에 맞는 구성 요소와 모델을 받습니다(5~20분)." "On the first run it downloads what suits this PC (5-20 minutes)."
