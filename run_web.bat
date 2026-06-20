@echo off
REM STELLAR SCREENER — ローカル Web サーバー起動（PowerShell ポリシー不要）
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [setup] 仮想環境 venv を作成しています...
    python -m venv venv
    if errorlevel 1 (
        echo Python が見つかりません。https://www.python.org/ から Python 3.11+ をインストールしてください。
        pause
        exit /b 1
    )
    echo [setup] 依存ライブラリをインストールしています...
    venv\Scripts\python.exe -m pip install --upgrade pip
    venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo.
echo STELLAR SCREENER を起動します: http://127.0.0.1:8000
echo 停止するには Ctrl+C を押してください。
echo.

venv\Scripts\python.exe -m uvicorn web_app:app --reload --host 127.0.0.1 --port 8000
