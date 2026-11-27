# ============================================================================
# Start VeloPass + Cloudflare HTTPS Tunnel on Windows
# Supports both Quick HTTPS URL (*.trycloudflare.com) and Named Custom Domain!
# Usage:
#   1) Quick free HTTPS link:
#      powershell -ExecutionPolicy Bypass -File .\scripts\start_https_tunnel.ps1
#   2) Custom Domain via Cloudflare Zero Trust Token:
#      $env:CLOUDFLARE_TUNNEL_TOKEN="eyJh..."; .\scripts\start_https_tunnel.ps1
# ============================================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $ProjectRoot "tools"
$CloudflaredExe = Join-Path $ToolsDir "cloudflared.exe"

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path $CloudflaredExe)) {
        New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
        Write-Host "[VeloPass] Downloading official cloudflared Windows binary..." -ForegroundColor Cyan
        Invoke-WebRequest `
            -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
            -OutFile $CloudflaredExe
    }
} else {
    $CloudflaredExe = (Get-Command cloudflared).Source
}

if ($env:CLOUDFLARE_TUNNEL_TOKEN) {
    Write-Host "[VeloPass] Starting Named Cloudflare HTTPS Tunnel (Custom Domain)..." -ForegroundColor Green
    & $CloudflaredExe tunnel --no-autoupdate run --token $env:CLOUDFLARE_TUNNEL_TOKEN
} else {
    Write-Host "[VeloPass] Starting Quick HTTPS Tunnel -> http://127.0.0.1:5000 ..." -ForegroundColor Green
    & $CloudflaredExe tunnel --no-autoupdate --url http://127.0.0.1:5000
}
