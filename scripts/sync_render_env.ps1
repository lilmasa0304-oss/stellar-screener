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
#   -Deploy        環境変数反映後に再デプロイをトリガーする (デフォルト: $true)

param(
    [string]$ServiceName = "stellar-screener",
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) ".env"),
    [bool]$Deploy = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$apiKey = $env:RENDER_API_KEY
if (-not $apiKey) {
    Write-Error @"
RENDER_API_KEY が未設定です。
Dashboard -> Account Settings -> API Keys で発行し、次を実行してください:
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
    param(
        [string]$Method,
        [string]$Uri,
        $Body = $null
    )
    $params = @{
        Method  = $Method
        Uri     = $Uri
        Headers = $headers
    }
    if ($null -ne $Body) {
        $params.ContentType = "application/json"
        $params.Body = ($Body | ConvertTo-Json -Depth 5 -Compress)
    }
    return Invoke-RestMethod @params
}

function Set-RenderEnvVar {
    param(
        [string]$ServiceId,
        [string]$Key,
        [string]$Value
    )
    # Render API: PUT /v1/services/{serviceId}/env-vars/{envVarKey}
    # Body: { "value": "..." }
    Invoke-RenderApi -Method PUT -Uri "https://api.render.com/v1/services/$ServiceId/env-vars/$Key" -Body @{
        value = $Value
    } | Out-Null
}

function Remove-RenderEnvVar {
    param(
        [string]$ServiceId,
        [string]$Key
    )
    Invoke-RenderApi -Method DELETE -Uri "https://api.render.com/v1/services/$ServiceId/env-vars/$Key" | Out-Null
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
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_FALLBACK_BASE_URL"
)

$existing = Invoke-RenderApi -Method GET -Uri "https://api.render.com/v1/services/$serviceId/env-vars?limit=100"
$existingKeys = @{}
foreach ($item in $existing) {
    if ($item.envVar) {
        $existingKeys[$item.envVar.key] = $true
    }
}

foreach ($key in $syncKeys) {
    if (-not $envMap.ContainsKey($key)) {
        Write-Host "  [skip] $key - .env に存在しません" -ForegroundColor DarkYellow
        continue
    }
    $value = $envMap[$key]
    if ($value -match "your_.*_here") {
        Write-Host "  [skip] $key - プレースホルダーのままです" -ForegroundColor DarkYellow
        continue
    }

    $action = if ($existingKeys.ContainsKey($key)) { "update" } else { "create" }
    Write-Host "  [$action] $key" -ForegroundColor Cyan
    Set-RenderEnvVar -ServiceId $serviceId -Key $key -Value $value
}

# 過去の typo キー (DIFY_APi_KEY) が残っていれば削除
$typoKey = "DIFY_APi_KEY"
if ($existingKeys.ContainsKey($typoKey)) {
    Write-Host "  [delete] $typoKey (typo)" -ForegroundColor DarkYellow
    Remove-RenderEnvVar -ServiceId $serviceId -Key $typoKey
}

Write-Host ""
Write-Host "環境変数の反映が完了しました。" -ForegroundColor Green

if ($Deploy) {
    Write-Host "再デプロイをトリガー中..." -ForegroundColor Cyan
    $deploy = Invoke-RenderApi -Method POST -Uri "https://api.render.com/v1/services/$serviceId/deploys" -Body @{
        clearCache = "do_not_clear"
    }
    Write-Host "  deploy id: $($deploy.id) / status: $($deploy.status)" -ForegroundColor Gray
    Write-Host "  1-3 分後に /health を確認してください。" -ForegroundColor Gray
} else {
    Write-Host "注意: Render API では環境変数変更後、再デプロイが必要です (-Deploy `$true)" -ForegroundColor DarkYellow
}

Write-Host "  https://stellar-screener.onrender.com/health" -ForegroundColor Gray
Write-Host '  openai_configured: true になれば OK' -ForegroundColor Gray
