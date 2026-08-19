"""前端每一支 `.js` 都必須是**合法的 ES module**。

⚠️ 這支測試存在的理由是實際踩過的坑（M3 B4 那一輪）：改寫
`creatorOpts` 時只換掉運算式的**尾行**，漏了外層還開著的 `.concat(`。
結果是 `accounts.js` 整支語法錯誤 —— 而症狀是：

  · 畫面：篩選器全變成純文字、媒體格線一片空白、分頁點了沒反應
  · console：**一行紅字都沒有**
  · `main.js` 的 boot-error 攔截器：不會觸發（模組根本沒被執行到）

也就是說，這種故障**現有的測試一條都抓不到**，而且看起來很像「資料是空的」。

當時本地是用 `node --check` 驗的，它**沒有攔下來**：對副檔名 `.js` 的檔案，
node 預設當成 CommonJS script 解析，那條路徑不會踩到同一個語法規則。
所以這裡刻意先複製成 `.mjs` 再驗 —— 副檔名決定 node 用哪個解析器。

這支測試**不需要瀏覽器**，但需要 `node`（開發環境本來就有，extension 的
5 套測試也靠它）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "snsmediadl" / "web" / "js", ROOT / "extension"]

NODE = shutil.which("node")


def _sources() -> list[Path]:
    out: list[Path] = []
    for base in TARGETS:
        out += [p for p in sorted(base.rglob("*.js")) if "node_modules" not in p.parts]
    return out


@pytest.mark.skipif(NODE is None, reason="環境裡沒有 node")
def test_every_web_js_parses_as_an_es_module(tmp_path: Path) -> None:
    """每一支前端 JS 都要能被當成 ES module 解析。

    ⚠️ **一定要複製成 `.mjs` 再驗。** 直接 `node --check foo.js` 會用
    CommonJS 的解析器，抓不到同一個語法錯誤 —— 那正是這支測試要防的漏網之魚。
    """
    sources = _sources()
    assert sources, "一支 JS 都掃不到 —— 這條測試已經失效了（路徑改了？）"

    bad: list[str] = []
    for src in sources:
        # 路徑攤平成檔名，避免不同目錄下的同名檔互相覆蓋
        rel = src.relative_to(ROOT).as_posix().replace("/", "__")
        copy = tmp_path / (rel + ".mjs")
        copy.write_bytes(src.read_bytes())
        r = subprocess.run(
            [NODE, "--check", str(copy)], capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).strip().splitlines()
            # node 會印出出錯的那一行與一個插入符號，留前幾行就夠指出位置
            bad.append(f"{src.relative_to(ROOT).as_posix()}\n      "
                       + "\n      ".join(detail[:6]))

    assert not bad, (
        "這些前端 JS 不是合法的 ES module。\n"
        "症狀非常惡劣：整支模組不執行，畫面像「資料是空的」，"
        "而 console 一行紅字都沒有：\n  " + "\n  ".join(bad)
    )
