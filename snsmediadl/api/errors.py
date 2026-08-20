"""API 錯誤：機器讀的 `code` + 人讀的英文 `detail`。

為什麼要 code
-------------
前端原本直接把 `detail` 印在畫面上，所以錯誤訊息**同時**是使用者介面文案。
那有兩個後果：

1. 三語系化之後，後端講中文、介面講英文 —— 而後端不知道使用者選了什麼語言，
   也不該知道（它是本機服務，一個 process 服務不同語言的分頁）。
2. 測試綁在文案上。改一個字就紅一批測試，於是文案不敢改。

拆開之後：`code` 是契約（測試斷言它、前端查表用它），`detail` 是**後備**
（前端查不到那個 code 時原樣顯示，看得出來是沒被翻譯的英文）。

⚠️ **`detail` 一律英文。** 它會出現在畫面上，而畫面可能是任何一個語系；
英文至少是這個專案裡每個開發者都讀得懂的那一個。

⚠️ **不要為了「安全」而模糊化 detail。** 這是本機單機服務（只綁 localhost、
不做認證），訊息裡的路徑與欄位名正是使用者診斷問題唯一的線索。

code 的命名
-----------
`<領域>.<情況>`，都用小寫底線：`media.not_downloaded`、`file.outside_root`、
`thumb.ffmpeg_missing`、`query.bad_sort`。領域對應 API 模組或資源，不對應
HTTP 狀態碼 —— 同一個狀態碼底下往往有好幾種完全不同的情況，而使用者要知道的
正是「是哪一種」。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
# ⚠️ **Starlette 的那一個，不是 FastAPI 的。** FastAPI 的 `HTTPException` 是
# 它的子類，而路由自己丟的 404 / 405 是**父類**。Starlette 依 `type(exc).__mro__`
# 找 handler，所以掛在子類上時父類的例外找不到我們，會落回內建 handler ——
# 症狀是「大部分錯誤有 code，但 404 沒有」，而前端讀到 undefined。
from starlette.exceptions import HTTPException


class ApiError(HTTPException):
    """帶 `code` 的 HTTPException。

    仍然繼承 `HTTPException`，所以既有的 `except HTTPException: raise`
    （例如 `files.py` 的縮圖包裝）不必改，也不會被誤蓋成 500。
    """

    def __init__(self, code: str, detail: str, status_code: int = 422) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


async def _handle(_request: Request, exc: Exception) -> JSONResponse:
    """回 `{"code": ..., "detail": ...}`。

    ⚠️ 沒有 code 的 `HTTPException`（FastAPI 自己丟的、還沒改完的舊呼叫點）
    也走這裡，`code` 給 `null` —— **不硬編一個假的 code**。前端看到 null 就
    退回顯示 detail，那正是「查不到就顯示原文」那條路。
    """
    assert isinstance(exc, HTTPException)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": getattr(exc, "code", None), "detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


def install(app: FastAPI) -> None:
    """把 handler 掛上去。`create_app()` 呼叫一次。

    掛在 `HTTPException` 上而不是只掛 `ApiError`：回應形狀要**一致**。
    兩種形狀的話前端得寫 `body.code ?? body.detail ?? ...` 那種到處都要防的
    程式碼，而漏掉的那一處就是一個顯示空白的錯誤訊息。
    """
    app.add_exception_handler(HTTPException, _handle)
