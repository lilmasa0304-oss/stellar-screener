# STELLAR SCREENER (株価スクリーナー)

JPX日経400を対象に、`yfinance` で株価データを取得・テクニカル分析し、BUY SIGNAL を **Web ダッシュボード上でその場表示** する Python アプリです。

## 主な機能

1. **テクニカルスクリーニング**
   - RSI（相対力指数）、移動平均線クロス、ボリンジャーバンド等
   - 押し目シグナル / 順張りブレイクの2モード
2. **JPX400 手動スキャン**
   - ボタン操作で約400銘柄をリアルタイム走査
   - 結果はアプリ内に即時表示
3. **統合AI診断チャット**
   - OpenAI API（gpt-4o 等）によるテクニカル + ファンダメンタルズの総合診断

---

## セットアップ

### 1. 仮想環境と依存ライブラリ

```bash
python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -r requirements.txt
```

または `run_web.bat` をダブルクリック（初回は venv 作成・依存インストールを自動実行）。

### 2. 環境変数（任意）

```bash
copy .env.template .env
```

| 変数 | 説明 |
|---|---|
| `OPENAI_API_KEY` | OpenAI API キー（統合AI診断に必須） |
| `OPENAI_MODEL` | 使用モデル（デフォルト: `gpt-4o`） |
| `DB_PATH` | SQLite パス（デフォルト: `data/screener.db`） |

### 3. 設定ファイル (`config.yaml`)

スクリーニング対象銘柄や判定しきい値を編集します。日本株は末尾に `.T` を付けます（例: `7203.T`）。

---

## 実行方法

### Web ダッシュボード（推奨）

```bash
run_web.bat
# または
python -m uvicorn web_app:app --reload --host 127.0.0.1 --port 8000
```

ブラウザで http://127.0.0.1:8000 を開きます。

### CLI スクリーニング

```bash
# ウォッチリスト（config.yaml の tickers）
python main.py

# JPX400 全銘柄
python main.py --jpx400

# 結果をコンソールに出力
python main.py --dry-run
```

---

## デプロイ

Render 向け設定は `render.yaml` を参照してください。本番 URL 例: https://stellar-screener.onrender.com/
