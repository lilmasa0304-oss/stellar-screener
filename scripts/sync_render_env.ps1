# .env の機密値を Render 本番サービスへ一括反映する（Render API 使用）
#
# 事前準備:
#   1. https://dashboard.render.com/u/settings#api-keys で API Key を発行
#   2. PowerShell で:
#        $env:RENDER_API_KEY = "rnd_xxxxxxxx"
#        .\scripts\sync_render_env.ps1
#
# オプション:
#   -ServiceName  stellar-screener  (デフォルト)
#   -EnvFile       ..\.env           (デフォルト)

param(
    [string]$ServiceName = "stellar-screener",
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) ".env")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$apiKey = $env:RENDER_API_KEY
if (-not $apiKey) {
    Write-Error @"
RENDER_API_KEY が未設定です。
Dashboard → Account Settings → API Keys で発行し、次を実行してください:
  `$env:RENDER_API_KEY = "rnd_xxxxxxxx"
  .\scripts\sync_render_env.ps1
"@
}

if (-not (Test-Path $EnvFile)) {
    Write-Error ".env が見つかりません: $EnvFile`ncopy .env.template .env して値を入力してください。"
}

$headers = @{
    Authorization = "Bearer $apiKey"
    Accept        = "application/json"
}

function Invoke-RenderApi {
    param([string]$Method, [string]$Uri, $Body = $null)
    $params = @{
        Method  = $Method
        Uri     = $Uri
        Headers = $headers
    }
    if ($Body) {
        $params.ContentType = "application/json"
        $params.Body = ($Body | ConvertTo-Json -Depth 5 -Compress)
    }
    return Invoke-RestMethod @params
}

Write-Host "Render サービス一覧を取得中..." -ForegroundColor Cyan
$services = Invoke-RenderApi -Method GET -Uri "https://api.render.com/v1/services?limit=100"
$service = $services | ForEach-Object { $_.service } | Where-Object { $_.name -eq $ServiceName } | Select-Object -First 1

if (-not $service) {
    Write-Error "サービス '$ServiceName' が見つかりません。Render Dashboard で名前を確認してください。"
}

$serviceId = $service.id
Write-Host "対象サービス: $($service.name) ($serviceId)" -ForegroundColor Green

# .env をパース（# コメント・空行を除外）
$envMap = @{}
Get-Content $EnvFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    if ($val.StartsWith('"') -and $val.EndsWith('"')) { $val = $val.Substring(1, $val.Length - 2) }
    $envMap[$key] = $val
}

$syncKeys = @(
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_USER_ID",
    "DIFY_API_KEY",
    "DIFY_BASE_URL"
)

$existing = Invoke-RenderApi -Method GET -Uri "https://api.render.com/v1/services/$serviceId/env-vars?limit=100"
$existingKeys = @{}
foreach ($item in $existing) {
    if ($item.envVar) { $existingKeys[$item.envVar.key] = $item.envVar.id }
}

foreach ($key in $syncKeys) {
    if (-not $envMap.ContainsKey($key)) {
        Write-Host "  [skip] $key — .env に存在しません" -ForegroundColor DarkYellow
        continue
    }
    $value = $envMap[$key]
    if ($value -match "your_.*_here") {
        Write-Host "  [skip] $key — プレースホルダーのままです" -ForegroundColor DarkYellow
        continue
    }

    if ($existingKeys.ContainsKey($key)) {
        $varId = $existingKeys[$key]
        Write-Host "  [update] $key" -ForegroundColor Cyan
        Invoke-RenderApi -Method PUT -Uri "https://api.render.com/v1/services/$serviceId/env-vars/$varId" -Body @{
            envVar = @{ key = $key; value = $value }
        } | Out-Null
    } else {
        Write-Host "  [create] $key" -ForegroundColor Cyan
        Invoke-RenderApi -Method POST -Uri "https://api.render.com/v1/services/$serviceId/env-vars" -Body @{
            envVar = @{ key = $key; value = $value }
        } | Out-Null
    }
}

Write-Host ""
Write-Host "環境変数の反映が完了しました。" -ForegroundColor Green
Write-Host "Render が自動再デプロイします（1〜3分）。完了後に確認:" -ForegroundColor Gray
Write-Host "  https://stellar-screener.onrender.com/health" -ForegroundColor Gray
Write-Host '  line_connected: true / dify_configured: true になれば OK' -ForegroundColor Gray
