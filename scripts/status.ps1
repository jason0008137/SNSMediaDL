# 佇列狀態。由 status.bat 呼叫。
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  SNSMediaDL 佇列狀態" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

python -m snsmediadl.cli status
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[X] 讀取失敗 — 資料庫可能還沒建立，先跑一次 start.bat" -ForegroundColor Red
}

Write-Host ""
Read-Host "按 Enter 關閉"
