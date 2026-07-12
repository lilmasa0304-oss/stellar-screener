# Render 本番ローンチ手順（月曜運用前・スマホから設定可）

## 判断: Render 継続 + Cloudflare AI Gateway（Railway 移行は不要）

- Railway 移行は環境再構築・検証に数時間かかり、明日までの確実性が低い
- Render はそのまま使い、**株価取得のフォールバック**と **OpenAI の Gateway 経由**で今夜中に直せる

---

## 今夜やること（15〜20分）

### 1. Cloudflare AI Gateway を作成（無料）

1. https://dash.cloudflare.com/ にログイン（アカウント作成可）
2. 左メニュー **AI** → **AI Gateway** → **Create Gateway**
3. Gateway 名を入力（例: `stellar-openai`）→ 作成
4. 作成後、**OpenAI** プロバイダのエンドポイント URL をコピー  
   形式: `https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY名>/openai`

### 2. Render に環境変数を追加

1. https://dashboard.render.com/ → **stellar-screener** → **Environment**
2. 以下を追加:

```
OPENAI_FALLBACK_BASE_URL=https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY名>/openai
```

3. **Save Changes** → 再デプロイが自動開始

### 3. デプロイ完了後の確認（スマホブラウザで可）

1. `https://stellar-screener.onrender.com/api/openai/probe`  
   - `reachable: true`  
   - `recommended_base_url` が Gateway URL になっていること
2. `https://stellar-screener.onrender.com/health`  
   - `openai_fallback_base_url` が設定されていること
3. アプリで **7203** と **1605** の診断をテスト

---

## コード側で今夜入る対策（自動デプロイ）

| 対策 | 内容 |
|------|------|
| 株価フォールバック | yfinance 失敗時 → Yahoo Chart API → Stooq |
| OpenAI Render 専用 | `OPENAI_FALLBACK_BASE_URL` 設定時は Gateway のみ使用 |
| 起動プローブ | サーバー起動時に到達可能ルートを自動選択 |

---

## まだ失敗する場合

- Render ログで `OpenAI 起動プローブ` と `Yahoo Chart API` / `Stooq` の行を確認
- `OPENAI_API_KEY` が有効か OpenAI ダッシュボードで確認
- Cloudflare Gateway のログでリクエストが通っているか確認
