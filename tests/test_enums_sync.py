"""三份 `ContentType` 清單必須一致。

同一個值域現在寫在四個地方：

  1. `snsmediadl/db/enums.py`        —— 真相
  2. `snsmediadl/web/js/enums.js`    —— GUI 的下拉
  3. `extension/bar.js`              —— extension 的下拉
  4. alembic 的 CHECK constraint     —— 由 1 產生，這裡不驗

加一個值要改三處（第 4 個跟著 migration 走）。**漏掉一處不會報錯**：
症狀是「這裡選得到、那裡選不到」，而使用者只會覺得某個下拉怪怪的。

實際發生過：`3d` 與 `photograph` 加進後端與 GUI 之後，extension 漏了半年
（2026-08-16 才發現）。所以這條測試不是潔癖，是補一道本來就該有的檢查。

⚠️ 前端兩份是用正規表示式抽的。**抽不到東西就要失敗**，不可以視為「沒有值＝
一致」—— 那會讓這條測試在檔案改寫之後靜默失效。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from snsmediadl.db.enums import ContentType, Rating

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _options(html: str, select_id: str) -> list[str]:
    """抽出某個 `<select>` 底下所有 `<option value="…">` 的值（空值不算）。"""
    block = re.search(
        rf'<select id="{select_id}"(.*?)</select>', html, re.S)
    assert block, f"找不到 <select id={select_id}>"
    return [v for v in re.findall(r'<option value="([^"]*)"', block.group(1)) if v]


def test_extension_content_type_matches_backend():
    html = (ROOT / "extension" / "bar.js").read_text(encoding="utf-8")
    got = _options(html, "content")
    assert got, "extension 的類型下拉抽不到任何選項 —— 這條測試已經失效了"
    assert got == ContentType.values(), (
        f"extension 的類型下拉與 db/enums.py 不一致：\n"
        f"  少了：{sorted(set(ContentType.values()) - set(got))}\n"
        f"  多了：{sorted(set(got) - set(ContentType.values()))}"
    )


def test_extension_rating_matches_backend():
    html = (ROOT / "extension" / "bar.js").read_text(encoding="utf-8")
    got = _options(html, "rating")
    assert got == Rating.values(), f"extension 的分級下拉與後端不一致：{got}"


@pytest.mark.parametrize("name,values", [
    ("CONTENTS", ContentType.values()),
    ("RATINGS", Rating.values()),
])
def test_web_enums_match_backend(name, values):
    js = (ROOT / "snsmediadl" / "web" / "js" / "enums.js").read_text(encoding="utf-8")
    m = re.search(rf"export const {name} = \[(.*?)\];", js, re.S)
    assert m, f"web/js/enums.js 裡找不到 {name} —— 這條測試已經失效了"
    got = [v for v in re.findall(r"'([^']*)'", m.group(1)) if v]
    assert got == values, (
        f"web/js/enums.js 的 {name} 與 db/enums.py 不一致：\n"
        f"  少了：{sorted(set(values) - set(got))}\n"
        f"  多了：{sorted(set(got) - set(values))}"
    )
