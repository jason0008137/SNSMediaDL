#!/usr/bin/env bash
# 前端模組的語法檢查。
#
# ⚠️ **不要直接用 `node --check foo.js`。**
# 那會把檔案當 CommonJS 解析，而 CommonJS 與 ESM 的作用域規則不同 ——
# 實測：一個檔案裡宣告兩次 `const FETCH_BAD` 在 ESM 是 SyntaxError，
# 但 `node --check` 回報 OK，結果整個 GUI 開起來是白的、console 還沒紅字。
#
# 複製成 .mjs 再檢查，才是模組語意。
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail=0
while IFS= read -r f; do
  cp "$f" "$tmp/$(echo "${f#"$root/"}" | tr '/' '_').mjs"
done < <(find "$root/snsmediadl/web/js" -name '*.js')

for m in "$tmp"/*.mjs; do
  if ! out=$(node --check "$m" 2>&1); then
    echo "✗ ${m##*/}"
    echo "$out" | head -5
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "✓ web/js 全部通過 ESM 語法檢查"
fi
exit "$fail"
