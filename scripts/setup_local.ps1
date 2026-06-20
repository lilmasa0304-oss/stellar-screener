# 初回セットアップ: venv 作成 + requirements インストール
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "Python:" (python --version)
python -m venv venv
$py = Join-Path $root "venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt
Write-Host "セットアップ完了。起動: .\run_web.bat  または  python main.py --web" -ForegroundColor Green
