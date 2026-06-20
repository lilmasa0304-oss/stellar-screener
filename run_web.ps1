# STELLAR SCREENER — ローカル Web サーバー起動
# 実行ポリシーエラー回避: python -m 経由で venv を直接呼び出す（Activate.ps1 不要）
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "[setup] 仮想環境 venv を作成しています..." -ForegroundColor Cyan
    python -m venv venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
}

Write-Host ""
Write-Host "STELLAR SCREENER: http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "停止: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

& $python -m uvicorn web_app:app --reload --host 127.0.0.1 --port 8000
