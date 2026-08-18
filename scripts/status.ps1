# 佇列狀態。由 status.bat 呼叫。
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
. (Join-Path $PSScriptRoot '_venv.ps1')

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  SNSMediaDL 佇列狀態" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$py = Resolve-VenvPython -Root $root
if (-not $py) {
    Write-Host "[X] 找不到 .venv — 先跑一次 start.bat" -ForegroundColor Red
    Write-Host ""
    Read-Host "按 Enter 關閉"
    exit 1
}

& $py -m snsmediadl.cli status
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[X] 讀取失敗 — 資料庫可能還沒建立，先跑一次 start.bat" -ForegroundColor Red
}

Write-Host ""
Read-Host "按 Enter 關閉"
