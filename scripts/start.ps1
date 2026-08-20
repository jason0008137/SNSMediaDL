# SNSMediaDL 一鍵啟動。由 start.bat 呼叫。
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail($msg) {
    Write-Host ""
    Write-Host "[X] $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "按 Enter 關閉"
    exit 1
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  SNSMediaDL Backend" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# --- Python：一律用專案自己的 .venv，不用全域 ---
#
# 全域環境跟一整套 ML/AI 工具鏈共用（見 scripts/_venv.ps1 的說明）。
# .venv 不存在時這裡會自己建，使用者不必知道 venv 是什麼。
. (Join-Path $PSScriptRoot '_venv.ps1')

$py = Resolve-VenvPython -Root $root
if (-not $py) { Fail "找不到 python，或建立 .venv 失敗。請安裝 Python 3.10 以上並加入 PATH。" }

$version = (& $py -c "import sys; print('%d.%d' % sys.version_info[:2])")
Write-Host "[1/3] Python $version (.venv)" -NoNewline

# --- 相依套件（缺了才裝，不要每次啟動都跑 pip）---
if (-not (Test-VenvDeps $py)) {
    Write-Host " — 安裝相依套件中（第一次會花一點時間）..."
    & $py -m pip install -e ".[dev,thumbs]"
    if ($LASTEXITCODE -ne 0) { Fail "套件安裝失敗。" }
} else {
    Write-Host " — 套件已就緒"
}

# --- DB migration ---
Write-Host "[2/3] 更新資料庫 schema..."
& $py -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "資料庫 migration 失敗。" }

# --- 啟動 ---
Write-Host "[3/3] 啟動伺服器"
Write-Host ""
Write-Host "  API 文件   http://127.0.0.1:8765/docs" -ForegroundColor Green
Write-Host "  佇列狀態   http://127.0.0.1:8765/api/queue/status" -ForegroundColor Green
Write-Host ""
# 不要在這裡宣稱「背景下載已啟用」—— auto_download 預設是關的，實際狀態
# 由 cli serve 啟動時如實印出。extension 的「送出並下載」走 POST /api/queue/run
# 明確觸發，不依賴背景下載開關。
Write-Host "  extension 按「送出並下載」即可入庫並下載。"
Write-Host "  停止請按 Ctrl+C。"
Write-Host ""

& $py -m snsmediadl.cli serve

Write-Host ""
Write-Host "伺服器已停止。"
Read-Host "按 Enter 關閉"
