"""`app.css` 的結構健檢：語法錯誤不可以靜默吃掉 token。

⚠️ 這支測試存在的理由是實際踩過的坑（`be40419`）：兩段註解合併時，
前一段的 `*/` 留著沒刪，於是

    /* ...（會破壞「本機單機、零外部相依」）。 */
       Label 與 Title 的字重是 500、Body 是 400 ——
       ...元件仍得各自寫死字重。 */
    --md-sys-typescale-label-small-size: 11px;

中間那兩行變成 `:root {}` 裡的**裸文字**。CSS 的錯誤恢復會一路跳到下一個
`;`，於是把緊接著的 `--md-sys-typescale-label-small-size` 一起吞掉 ——
該 token 變成空值，33 處引用它的 `font-size` 全部靜默失效。

沒有錯誤訊息、沒有例外、畫面只是「字看起來怪怪的」。這正是本專案
「禁止用 fallback 掩蓋問題」要防的那種故障：靜默、延遲、難追。

這支測試**不需要瀏覽器也不需要 node**：純文字比對。
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "snsmediadl" / "web" / "app.css"

DECL = re.compile(r"^\s*--[A-Za-z0-9-]+\s*:[^;]*;\s*$")


def _strip_comments(text: str) -> str:
    """把 `/* … */` 換成等長的空白，行號與行結構原封不動。"""
    out = []
    i = 0
    while i < len(text):
        start = text.find("/*", i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find("*/", start + 2)
        assert end > 0, f"註解沒有收尾：{CSS.name} 第 {text.count(chr(10), 0, start) + 1} 行"
        # 保留換行，其餘變空白 —— 這樣報錯時行號還是對得上原始檔。
        out.append("".join("\n" if c == "\n" else " " for c in text[start : end + 2]))
        i = end + 2
    return "".join(out)


def test_no_stray_comment_terminator() -> None:
    """剝掉成對註解之後，不該還有落單的 `*/`。

    落單的 `*/` 就是「多關了一次」——前面那段文字已經不在註解裡了。
    """
    stripped = _strip_comments(CSS.read_text(encoding="utf-8"))
    strays = [i + 1 for i, ln in enumerate(stripped.splitlines()) if "*/" in ln]
    assert not strays, f"app.css 這幾行有落單的 `*/`（前面的文字其實沒被註解掉）：{strays}"


def test_root_block_has_no_bare_text() -> None:
    """`:root { … }` 裡除了宣告與大括號之外，不該有別的東西。

    裸文字會讓 CSS 的錯誤恢復吃掉它後面那一條宣告。
    """
    stripped = _strip_comments(CSS.read_text(encoding="utf-8"))
    start = stripped.index(":root")
    end = stripped.index("}", start)
    bad = []
    for offset, line in enumerate(stripped[start:end].splitlines()):
        text = line.strip()
        if not text or text.startswith(":root") or text == "{":
            continue
        if not DECL.match(line):
            bad.append((stripped.count("\n", 0, start) + offset + 1, text[:60]))
    assert not bad, f"`:root` 區塊裡有不是宣告的內容（會吃掉下一條宣告）：{bad}"


def test_every_declared_token_survives_parsing() -> None:
    """原始碼裡寫了幾個 token，剝掉註解後就該剩幾個。

    差額代表有 token 被寫在註解裡、或被語法錯誤吞掉了。
    """
    raw = CSS.read_text(encoding="utf-8")
    stripped = _strip_comments(raw)
    declared = {
        m.group(1)
        for m in re.finditer(r"^\s*(--[A-Za-z0-9-]+)\s*:", stripped, re.MULTILINE)
    }
    # 被引用但從沒宣告過的 token —— 拼錯名字就是這個症狀。
    used = set(re.findall(r"var\(\s*(--[A-Za-z0-9-]+)", stripped))
    missing = sorted(used - declared)
    assert not missing, f"這些 token 被 var() 引用卻沒有宣告（拼錯或被吃掉）：{missing}"


RULE = re.compile(r"^(\.[A-Za-z][A-Za-z0-9_-]*)\s*\{([^}]*)\}", re.MULTILINE | re.DOTALL)


def test_no_two_components_share_a_layout_class() -> None:
    """同一個 class 不該有兩處各自宣告佈局。

    ⚠️ 這支測試存在的理由也是實際踩過的坑：帳號頁的選取列與媒體頁的選取列
    都叫 `.sel-bar`，一個是 `flex-direction: column`、一個是單列 `flex-wrap`。
    兩份規則疊起來之後，後定義的 column 贏了，而前面那條的
    `align-items: center` 還留著 —— 媒體頁的選取列變成每個控制項各佔一行、
    全部置中。

    沒有錯誤訊息，CSS 也完全合法：它只是「兩個元件剛好同名」。
    """
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    seen: dict[str, list[int]] = {}
    for m in RULE.finditer(css):
        cls, body = m.group(1), m.group(2)
        if "display:" in body or "flex-direction:" in body:
            seen.setdefault(cls, []).append(css.count("\n", 0, m.start()) + 1)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, (
        "這些 class 有兩處各自宣告佈局，多半是兩個不同元件撞了名字"
        f"（把後來的那個改名）：{dupes}"
    )


# ── 排版四件組 ────────────────────────────────────────────
#
# M3 的一個排版角色是 size + line-height + weight + tracking 四件一組。
# 只寫 font-size 等於做了四分之一：字重與行高會掉回瀏覽器預設，整個介面
# 少一層層級感。app.css 因此為每個角色備了一個可餵給 `font` 簡寫的複合
# token（`--md-sys-typescale-label-large` 之類），元件端一次套齊。
#
# 這組測試守的是複合 token 特有的兩個靜默故障。

ROLE = re.compile(r"--md-sys-typescale-([a-z]+-[a-z]+)-size\s*:")
COMPOSITE_DECL = re.compile(
    r"^\s*--md-sys-typescale-([a-z]+-[a-z]+)\s*:([^;]*);", re.MULTILINE)


def _root(css: str) -> str:
    start = css.index(":root")
    return css[start : css.index("\n}\n", start)]


def test_every_typescale_role_has_a_composite_token() -> None:
    """有單項 token 的角色都要有對應的複合 token。

    少一個的症狀是元件端還在寫 `font-size:`，那個角色就永遠只做四分之一。
    """
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    root = _root(css)
    roles = set(ROLE.findall(root))
    composites = {role for role, _ in COMPOSITE_DECL.findall(root)}
    missing = sorted(roles - composites)
    assert not missing, f"這些排版角色沒有複合 token（元件端只能套四分之一）：{missing}"


def test_composite_tokens_hold_no_second_copy_of_the_numbers() -> None:
    """複合 token 只准由單項 token 組成，不准自己寫一份數值。

    寫死數值會出現兩份真相：改了 `--...-size` 而複合 token 沒跟著改，
    畫面上一半的元件動、一半的不動，而且完全沒有錯誤訊息。
    """
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    bad = []
    for role, value in COMPOSITE_DECL.findall(_root(css)):
        # 剝掉所有 var(...) 之後不該還剩下數字
        bare = re.sub(r"var\([^()]*\)", "", value)
        if re.search(r"\d", bare):
            bad.append((role, value.strip()))
    assert not bad, (
        "複合 token 裡有寫死的數值 —— 應該全部用單項 token 組成："
        f"{bad}"
    )


def test_no_component_still_sets_font_size_alone() -> None:
    """:root 之外不該再有 `font-size: var(--...-size)`。

    那正是「只做四分之一」的寫法：字重與行高會掉回瀏覽器預設。
    """
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    root_end = css.index("\n}\n", css.index(":root")) + 3
    leftovers = re.findall(
        r"font-size:\s*var\(--md-sys-typescale-[a-z-]+-size\)", css[root_end:])
    assert not leftovers, (
        "這幾處還在單獨套 font-size，字重與行高會掉回瀏覽器預設："
        f"{leftovers}"
    )


FONT_SHORTHAND = re.compile(r"font:\s*var\(--md-sys-typescale-")
# `font` 簡寫會**重設**這些屬性。寫在它前面 = 靜默被吃掉。
CLOBBERED = re.compile(
    r"\b(font-weight|font-family|font-style|font-variant|font-stretch)\s*:")


def test_nothing_the_font_shorthand_resets_is_written_before_it() -> None:
    """`font` 簡寫前面不可以有它會重設掉的屬性。

    ⚠️ 這支測試存在的理由是實際踩過的坑：`.brand` 的 `font-weight: 600`
    與 `.err-row .msg` 的 `font-family: ui-monospace` 都寫在 `font-size`
    前面。把 `font-size` 換成 `font` 簡寫之後，簡寫把它們一起重設回角色
    預設 —— 品牌字掉回 500、錯誤訊息掉回非等寬。

    CSS 完全合法，沒有警告，只是「看起來怪怪的」。
    """
    css = _strip_comments(CSS.read_text(encoding="utf-8"))
    bad = []
    for m in re.finditer(r"\{([^{}]*)\}", css):
        body = m.group(1)
        fm = FONT_SHORTHAND.search(body)
        if not fm:
            continue
        for cm in CLOBBERED.finditer(body[: fm.start()]):
            line = css.count("\n", 0, m.start(1) + cm.start()) + 1
            bad.append((line, cm.group(1)))
    assert not bad, (
        "這幾處把 font 簡寫會重設的屬性寫在簡寫**之前**，會被靜默吃掉"
        f"（把它搬到 `font:` 後面）：{bad}"
    )
