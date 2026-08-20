"""GUI 的程式碼裡不准出現中日文 —— 那代表有人繞過了語系檔。

為什麼是測試而不是 `publish/sync.ps1` 的掃描規則
------------------------------------------------
原本的計畫是在 `$Scan` 加一條 regex。試過之後改成測試，理由是**正確性**：

單行 regex 分不出「註解裡的中文」與「字串裡的中文」。這個 codebase 的註解
**刻意維持中文**（那是給開發者的），所以規則必須排除註解 —— 而
`const txt = sum.querySelector('.ms-text');   // 見 multiDrop` 這種行上，
任何「引號後面出現中文」的 regex 都會誤命中。永遠紅的規則等於沒有規則：
大家會學會忽略它。

這裡把註解真的剖析掉再檢查，所以判斷是準的。而且它跑在 pytest 裡（每次都跑），
不是只在發布前跑一次 —— 而 `sync.ps1` 的 `$Verification` 本來就會跑 pytest，
所以發布前照樣擋得到。

⚠️ 這支**不驗翻譯品質**，只驗「有沒有硬寫」。少一個 key、翻錯字，是
`js/test_i18n.mjs` 與實機驗收的事。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "snsmediadl" / "web"

# 中日文的字。**不含標點**（`、` `（`）—— 標點另有一組共用 key
# （`common.listsep` / `common.paren`），而符號（★ ♥ ⚠ ⊘）本來就該留在樣板側。
CJK = re.compile(r"[一-鿿぀-ヿ]")

# 語系檔理所當然有中日文；`dev/` 是開發用的測試跑者，不是產品畫面。
SKIP_DIRS = {"i18n", "dev"}

# 逐行的例外。**唯一的合法用途是「這個字本來就不該被翻譯」** ——
# 目前只有語言選單上各語言自己的名字（看不懂目前介面語言的人，正是最需要
# 在那裡找到自己語言的那個人，所以那三個名字在任何語系下都一樣）。
#
# ⚠️ 加一條之前先問自己：這個字在英文介面上出現是對的嗎？答案是「不對，但
# 我懶得抽」的話，那就是這條測試要擋的東西，不是例外。
# 標記寫在**該行或它上面那一行**。長的那一行擠不下註解時放上面比較好讀，
# 而只認同一行的話標記會被寫成一個看不見的陷阱（放上面沒作用、但沒有人會發現）。
EXEMPT = "i18n-exempt:"


def _exempt(lines: list[str], lineno: int) -> bool:
    window = lines[max(0, lineno - 2):lineno]
    return any(EXEMPT in line for line in window)


def _sources() -> list[Path]:
    out: list[Path] = []
    for pattern in ("*.js", "*.html"):
        for p in sorted(WEB.rglob(pattern)):
            if SKIP_DIRS & set(p.relative_to(WEB).parts):
                continue
            # `test_*.mjs` / `testkit.js` 的測試名稱是給開發者看的，與註解同性質。
            if p.name.startswith("test"):
                continue
            out.append(p)
    return out


def _js_code_only(text: str) -> list[str]:
    """每一行只留「不是註解」的部分。行數與原檔一致（回報行號要用）。

    ⚠️ **引號狀態每行重置。** 完整的 JS tokenizer 要處理 regex 字面值
    （`/[&<>"']/g` 裡有兩個引號）與樣板字串的巢狀 —— 判斷錯一次就會從那裡
    開始整份失準，而失準的方向是「把後面的註解當成程式碼」，也就是一堆
    誤報。每行重置的代價只有「同一行內引號沒配平時，那一行後半被當成字串」，
    而那一行本來就會被檢查到底。

    區塊註解（`/** … */`）**跨行追蹤** —— JSDoc 在這個 codebase 到處都是，
    不追蹤的話它們全部會被誤報。
    """
    out: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw
        kept: list[str] = []
        i = 0
        n = len(line)
        quote: str | None = None
        while i < n:
            ch = line[i]
            if in_block:
                if ch == "*" and i + 1 < n and line[i + 1] == "/":
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if quote:
                kept.append(ch)
                if ch == "\\":
                    if i + 1 < n:
                        kept.append(line[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in "'\"`":
                quote = ch
                kept.append(ch)
                i += 1
                continue
            if ch == "/" and i + 1 < n and line[i + 1] == "/":
                break                      # 行註解：這一行剩下的全丟掉
            if ch == "/" and i + 1 < n and line[i + 1] == "*":
                in_block = True
                i += 2
                continue
            kept.append(ch)
            i += 1
        out.append("".join(kept))
    return out


def _html_code_only(text: str) -> list[str]:
    """`<!-- -->` 換成等長空白，行結構不變。"""
    chars = list(text)
    for m in re.finditer(r"<!--.*?-->", text, re.S):
        for i in range(m.start(), m.end()):
            if text[i] != "\n":
                chars[i] = " "
    return "".join(chars).splitlines()


def test_no_cjk_left_in_the_gui_source() -> None:
    """畫面上的字一律走語系檔。註解維持中文 —— 那是給開發者的。"""
    sources = _sources()
    assert sources, "一支檔案都掃不到 —— 這條測試已經失效了（路徑改了？）"

    bad: list[str] = []
    for src in sources:
        text = src.read_text(encoding="utf-8")
        # ⚠️ JS 也要掃 `<!-- -->`：`innerHTML = \`…\`` 的樣板字串裡放 HTML
        # 註解是這個 codebase 的常態（overlay 的 body 都是那樣寫的），
        # 而那些一樣是註解。先拆 HTML 註解再拆 JS 註解。
        lines = (_html_code_only(text) if src.suffix == ".html"
                 else _js_code_only("\n".join(_html_code_only(text))))
        original = text.splitlines()
        for lineno, line in enumerate(lines, 1):
            if CJK.search(line) and not _exempt(original, lineno):
                bad.append(f"{src.relative_to(ROOT).as_posix()}:{lineno}: "
                           f"{original[lineno - 1].strip()[:100]}")

    assert not bad, (
        "GUI 的程式碼裡還有硬寫的中日文。畫面上的字一律走 i18n："
        "查表拿 key，要用的時候才 t()。\n  " + "\n  ".join(bad)
    )


def test_the_gate_actually_catches_a_hardcoded_string() -> None:
    """⚠️ **這條在驗上面那條是不是還活著。**

    「掃描全綠」只有在掃描還會紅的時候才有意義。`test_i18n` 的「不准有 HTML
    標記」就曾經靜默失效過（regex 裡的 `\\b` 在產生檔案時被跳脫吃掉），綠了
    好幾輪都沒有人發現。
    """
    sample = "\n".join([
        "// 這是註解，中文是給開發者看的，不該被抓",
        "/** JSDoc 也一樣：",
        " *  這幾行都是註解 */",
        "const a = 1;   // 行尾註解也不該被抓",
        "const ok = t('accounts.edit');",
        "const bad = '編輯';",
    ])
    hits = [i for i, line in enumerate(_js_code_only(sample), 1) if CJK.search(line)]
    assert hits == [6], f"應該只抓到第 6 行那個硬寫的字串，實際抓到 {hits}"
