"""前端的 `$('id')` 參照，在 `index.html` 裡都找得到嗎。

⚠️ 這支測試存在的理由是實際踩過的坑：改版時少留一個 id，`$()` 回 null，
接著第一次 `.value` 或 `.addEventListener` 就 TypeError —— 而那發生在模組
最上層，**整頁空白**，主控台以外看不到任何線索。

上一輪（`336ad1c` 回朔）是靠人工比對抓到的。這一輪動的 id 更多，改成機器檢查。

這支測試**不需要瀏覽器也不需要 node**：純文字比對。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "snsmediadl" / "web"

ID_ATTR = re.compile(r'id="([A-Za-z0-9_-]+)"')
DOLLAR_CALL = re.compile(r"""\$\(\s*['"]([A-Za-z0-9_-]+)['"]\s*\)""")


def _declared_ids() -> set[str]:
    """index.html 裡宣告的 id，加上 JS 樣板字串裡寫死的 id。

    後者也算「宣告」：`showDetail()` 產生的那一塊 HTML 就是這樣來的。
    """
    ids = set(ID_ATTR.findall((WEB / "index.html").read_text(encoding="utf-8")))
    for js in sorted(WEB.glob("js/**/*.js")):
        ids |= set(ID_ATTR.findall(js.read_text(encoding="utf-8")))
    return ids


def _referenced_ids() -> dict[str, set[str]]:
    """每個 JS 檔案用 `$('...')` 參照了哪些 id。"""
    out: dict[str, set[str]] = {}
    for js in sorted(WEB.glob("js/**/*.js")):
        found = set(DOLLAR_CALL.findall(js.read_text(encoding="utf-8")))
        if found:
            out[js.relative_to(WEB).as_posix()] = found
    return out


def test_every_referenced_id_exists() -> None:
    declared = _declared_ids()
    missing: list[str] = []
    for path, ids in _referenced_ids().items():
        for i in sorted(ids - declared):
            missing.append(f"{path}: $('{i}')")
    assert not missing, (
        "這些 id 在程式裡被參照，但 index.html 與 JS 樣板裡都沒有宣告。\n"
        "少一個 id 的症狀是整頁空白（$() 回 null，第一次存取就 TypeError）：\n  "
        + "\n  ".join(missing)
    )


def test_new_filter_and_sort_controls_are_wired() -> None:
    """這一輪改版新增的控制項，HTML 與 JS 兩邊都要在。

    分開寫一條的理由：上面那條只保證「參照到的都存在」，但**整個控制項被
    漏掉**（HTML 有、JS 沒接）它抓不到 —— 而那正是回朔重做時最容易發生的事。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    media_js = (WEB / "js" / "views" / "media.js").read_text(encoding="utf-8")

    for i in ("fSortKey", "fSortDir", "sortNote", "fRating", "fContent", "fKind"):
        assert f'id="{i}"' in html, f"index.html 少了 {i}"

    # 舊的三選項排序下拉已經整個換掉，不該有殘留
    assert 'id="fSort"' not in html, "fSort 是舊的排序下拉，應已被 fSortKey + fSortDir 取代"

    # 多值一定要用 append。用 set 的症狀是「勾三個只篩到最後一個」，
    # 而畫面上的標籤是對的 —— 看起來像後端壞了。
    assert "p.append(f.param" in media_js, "多值篩選必須用 append，不是 set"


# ── 表單語意：`.value` / change 只對真正的表單控制項成立 ──────────
#
# ⚠️ 這一組守的是**比缺 id 更難查**的那一種故障。
#
# `test_every_referenced_id_exists` 只保證 `$('x')` 不會回 null。但把
# `<select id="fSortKey">` 換成 `<span id="fSortKey">`（自製下拉的容器）之後，
# `$('fSortKey')` 回的是**一個元素**，於是：
#
#     $('fSortKey').value                        // undefined —— 條件靜默消失
#     $('fSortKey').addEventListener('change')   // 掛得上、永遠不觸發
#
# 兩者都不報錯、console 全白。症狀分別是「篩選沒生效」與「換了沒反應」，
# 而畫面上的控制項看起來完全正常。
#
# 這一輪（B2/B3）把 8 個原生 `<select>` 全部換掉，這條測試就是那次改動的守門員。

TAGGED_ID = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*\bid="([A-Za-z0-9_-]+)"')

FORM_TAGS = {"input", "select", "textarea"}

# `$('x').value` / `.checked` / `.addEventListener('change'|'input'` —— 全部是
# 只有原生表單控制項才有的東西。
FORM_USE = re.compile(
    r"""\$\(\s*['"]([A-Za-z0-9_-]+)['"]\s*\)\s*\."""
    r"""(value\b|checked\b|addEventListener\(\s*['"](?:change|input)['"])"""
)


def _id_tags() -> dict[str, set[str]]:
    """id -> 它被宣告成哪些標籤（index.html 與 JS 樣板字串都算）。"""
    out: dict[str, set[str]] = {}
    files = [WEB / "index.html", *sorted(WEB.glob("js/**/*.js"))]
    for f in files:
        for tag, i in TAGGED_ID.findall(f.read_text(encoding="utf-8")):
            out.setdefault(i, set()).add(tag.lower())
    return out


def test_form_only_apis_are_used_on_form_controls() -> None:
    """`.value` / `.checked` / change 監聽只准用在 input/select/textarea 上。

    用在自製下拉的容器（`<span>` / `<div>`）上不會報錯，只會靜默失效。
    正解是走下拉握把的 `get()` / `set()` / `onChange`。
    """
    tags = _id_tags()
    bad: list[str] = []
    for js in sorted(WEB.glob("js/**/*.js")):
        text = js.read_text(encoding="utf-8")
        for i, use in FORM_USE.findall(text):
            declared = tags.get(i)
            # 只在 JS 裡動態建立、沒有標籤可查的就跳過 —— 判斷不了
            if not declared:
                continue
            if declared & FORM_TAGS:
                continue
            bad.append(
                f"{js.relative_to(WEB).as_posix()}: $('{i}').{use}… "
                f"但 {i} 是 <{'/'.join(sorted(declared))}>"
            )
    assert not bad, (
        "這些地方對**非表單元素**用了只有表單控制項才有的 API。\n"
        "不會報錯，只會靜默失效（.value 回 undefined、change 永遠不觸發）。\n"
        "自製下拉要走握把的 get() / set() / onChange：\n  "
        + "\n  ".join(bad)
    )


# 註解與 JSDoc 裡會提到 `<select>`（解釋為什麼不用它），那些不算違規。
COMMENT_LINE = re.compile(r"^\s*(//|\*|/\*)")


def test_no_native_select_is_left_anywhere() -> None:
    """`index.html` 與 **JS 樣板字串**裡都不該再有原生 `<select>`。

    原生 select 的下拉箭頭與展開後的清單是**作業系統畫的**，不受 M3 token
    控制。同一列裡放一個原生 select 和一個自製下拉，兩者長得完全不一樣 ——
    這是 M3 稽核裡 Components 失分的直接來源。

    ⚠️ 這條**必須連 JS 一起掃**。只掃 index.html 的版本會讓人以為做完了，
    而抽屜、詳情面板、批次列那 8 個是 `innerHTML` 塞進去的 —— 它們照樣
    出現在畫面上，只是 grep index.html 看不到。
    """
    bad: list[str] = []
    for f in [WEB / "index.html", *sorted(WEB.glob("js/**/*.js"))]:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "<select" in line and not COMMENT_LINE.match(line):
                bad.append(f"{f.relative_to(WEB).as_posix()}:{i}: {line.strip()[:70]}")
    assert not bad, (
        "還有原生 <select>。改用 dom.js 的 singleDrop()（單選）或 "
        "multiDrop()（多選）；動態產生的樣板用 mountDrops() 在重畫之後掛上：\n  "
        + "\n  ".join(bad)
    )
