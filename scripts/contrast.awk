# WCAG 2.1 相對亮度與對比度計算。
# 純 awk，無外部相依 —— 公司電腦沒有 Python / Node 也跑得動。
#
#   awk -f scripts/contrast.awk scripts/contrast-pairs.txt
#
# 輸入每行： <前景hex> <背景hex> <標籤>
# 標籤的前綴決定判準（不寫前綴 = 文字用途）：
#
#   （無前綴） 文字      需 >= 4.5:1
#   ui:        UI 元件   需 >= 3:1（邊界、圖形、狀態指示）
#   deco:      裝飾      無門檻，只報數字（例如純分隔線）
#   grey:      灰階可分  不看對比度，看**相對亮度比** —— 需 >= 1.5x
#
# 為什麼要有 grey：M3 的同一個 tone 就是同一個 L*，所以 dark scheme 的
# primary / error / star / fav / ok / warn 在灰階下同階。凡是靠亮度差
# 當唯一區分通道的地方（例如 ★ 的亮/暗，同一個字符沒有形狀通道），
# 要驗的是亮度比，不是 WCAG 對比度 —— 拿 4.5:1 去套會得到假 FAIL。

function hex2dec(s,   i, c, n, v) {
  n = 0
  for (i = 1; i <= length(s); i++) {
    c = tolower(substr(s, i, 1))
    v = index("0123456789abcdef", c) - 1
    n = n * 16 + v
  }
  return n
}

function chan(v) {                      # sRGB -> 線性
  v = v / 255
  if (v <= 0.04045) return v / 12.92
  return ((v + 0.055) / 1.055) ^ 2.4
}

function lum(h,   r, g, b) {
  gsub(/^#/, "", h)
  r = chan(hex2dec(substr(h, 1, 2)))
  g = chan(hex2dec(substr(h, 3, 2)))
  b = chan(hex2dec(substr(h, 5, 2)))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function ratio(f, b,   l1, l2, t) {
  l1 = lum(f); l2 = lum(b)
  if (l1 < l2) { t = l1; l1 = l2; l2 = t }
  return (l1 + 0.05) / (l2 + 0.05)
}

# 灰階等值：把相對亮度反推回 sRGB 的灰。用來證明兩色在灰階下是否同階。
function greyhex(h,   L, s, v) {
  L = lum(h)
  s = (L <= 0.0031308) ? L * 12.92 : 1.055 * (L ^ (1 / 2.4)) - 0.055
  v = int(s * 255 + 0.5)
  return sprintf("#%02X%02X%02X", v, v, v)
}

BEGIN { fails = 0; rows = 0 }

/^# / { next }
NF < 2 { next }

{
  fg = $1; bg = $2
  label = ""
  for (i = 3; i <= NF; i++) label = label (i > 3 ? " " : "") $i

  mode = "text"; need = 4.5
  if (label ~ /^ui:/)        { mode = "ui";   need = 3.0 }
  else if (label ~ /^deco:/) { mode = "deco"; need = 0 }
  else if (label ~ /^grey:/) { mode = "grey"; need = 1.5 }
  sub(/^(ui|deco|grey):[[:space:]]*/, "", label)

  rows++
  Lf = lum(fg); Lb = lum(bg)

  if (mode == "grey") {
    # 亮度比，不是 WCAG 對比度
    hi = (Lf > Lb) ? Lf : Lb
    lo = (Lf > Lb) ? Lb : Lf
    r = (lo > 0) ? hi / lo : 999
    ok = (r >= need)
    if (!ok) fails++
    printf "%-9s vs %-9s  L比 %5.2fx  %-12s  灰階 %s vs %s  %s\n",
           fg, bg, r, (ok ? "灰階可分" : "★灰階同階★"), greyhex(fg), greyhex(bg), label
    next
  }

  r = ratio(fg, bg)
  if (mode == "deco") {
    verdict = "裝飾(無門檻)"
  } else {
    ok = (r >= need)
    if (!ok) fails++
    verdict = ok ? ((r >= 4.5) ? "AA-text" : "AA-UI") : "★FAIL★"
  }
  printf "%-9s on %-9s  %6.2f:1  %-12s  L(fg)=%.4f L(bg)=%.4f  %s\n",
         fg, bg, r, verdict, Lf, Lb, label
}

END {
  printf "\n%d 筆，%d 個 FAIL\n", rows, fails
  if (fails > 0) exit 1
}
