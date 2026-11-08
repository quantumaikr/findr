#!/usr/bin/env bash
# uv for the tests and the app bundle: Astral's own release server first, GitHub second.
# On 2026-11-08 GitHub release downloads answered 504 for hours while releases.astral.sh served the same files.
#   get-uv.sh TRIPLE OUT_DIR      -> OUT_DIR/uv (uv.exe on Windows)      UV_VERSION overrides the pinned version
set -u
V=${UV_VERSION:-0.13.7}; T=$1; OUT=$2
case "$T" in *windows*) E=zip ;; *) E=tar.gz ;; esac
F="uv-$T.$E"; tmp=$(mktemp -d); ok=""
for base in https://releases.astral.sh/github/uv/releases/download https://github.com/astral-sh/uv/releases/download; do
  for i in 1 2 3; do
    if curl -LsSf -o "$tmp/$F" "$base/$V/$F"; then ok=$base; break 2; fi
    echo "  $base: 실패 ($i)"; sleep 5
  done
done
[ -n "$ok" ] || { echo "uv $V를 받지 못했습니다"; exit 1; }
mkdir -p "$OUT"
if [ "$E" = zip ]; then
  (cd "$tmp" && unzip -o -q "$F") && mv "$tmp/uv.exe" "$OUT/" || { echo "uv 압축을 풀지 못했습니다"; exit 1; }
else
  tar -xzf "$tmp/$F" -C "$tmp" && mv "$tmp/uv-$T/uv" "$OUT/" || { echo "uv 압축을 풀지 못했습니다"; exit 1; }
fi
rm -rf "$tmp"
echo "uv $V ($T) <- $ok"
