# 專案自己的虛擬環境。由 start / update / status 三支 dot-source。
#
# ⚠️ 這個檔案必須存成 UTF-8 with BOM。Windows PowerShell 5.1 對沒有 BOM 的
#    檔案用系統 ANSI codepage 解讀，底下的中文會變成亂碼。
#
# 為什麼要有 venv：這台機器的全域 Python 裝了 235 個套件，torchvision /
# ultralytics / rembg / litellm / openai / mcp 都在裡面，而它們與本專案
# 共用 pydantic、Pillow、httpx、tomli。共用一個環境的意思是：任何一邊
# 為了自己升級一個大版本，另一邊可能在**下一次啟動時才發現壞掉**，
# 而症狀會出現在跟改動完全無關的地方。

# 找出專案的 .venv，沒有就建一個。回傳它的 python.exe 路徑，失敗回 $null。
#
# ⚠️ 失敗時**不自己 Fail** —— 三支呼叫端的 Fail 簽章不一樣
#    （start 是一個參數，update 是兩個），在這裡呼叫會炸在錯的地方。
function Resolve-VenvPython {
    param([Parameter(Mandatory)][string] $Root)

    $venv = Join-Path $Root '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'
    if (Test-Path $py) { return $py }

    # 系統 python 只在這裡用一次 —— 用來生出 venv。之後一律不碰它。
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sys) { return $null }

    Write-Host "  建立虛擬環境 .venv（第一次會花一點時間）..." -ForegroundColor DarkGray
    & $sys.Source -m venv $venv
    # 兩個條件都要檢查：venv 模組失敗時不一定給非零 exit code。
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { return $null }
    return $py
}

# 相依裝好了沒。回 $true / $false，**不安裝** —— 呼叫端自己決定要不要裝
# 以及要印什麼訊息。
function Test-VenvDeps {
    param([Parameter(Mandatory)][string] $Python)

    & $Python -c "import fastapi, sqlalchemy, alembic, httpx" 2>$null
    return ($LASTEXITCODE -eq 0)
}
