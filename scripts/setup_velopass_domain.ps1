# ============================================================================
# VeloPass Automated Custom Domain Provisioner (Cloudflare Named Tunnel)
# Automatically creates tunnel "velopass" and binds https://<domain> -> 127.0.0.1:5000
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_velopass_domain.ps1 -Hostname "velopass.yourdomain.com"
# ============================================================================
param(
    [Parameter(Mandatory = $false)]
    [string]$Hostname = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $ProjectRoot "tools"
$Cloudflared = Join-Path $ToolsDir "cloudflared.exe"

if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    $Cloudflared = (Get-Command cloudflared).Source
} elseif (-not (Test-Path $Cloudflared)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    Write-Host "[VeloPass] Tải công cụ cloudflared chính thức..." -ForegroundColor Cyan
    Invoke-WebRequest `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $Cloudflared
}

if (-not $Hostname) {
    $TunnelKey = Join-Path $ToolsDir "velopass_tunnel_key"
    if (-not (Test-Path $TunnelKey)) {
        New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
        ssh-keygen -t ed25519 -f $TunnelKey -q -N '""'
    }
    Write-Host "=================================================================" -ForegroundColor Cyan
    Write-Host " KHỞI CHẠY ĐƯỜNG DẪN HTTPS MANG TÊN VELOPASS (CUSTOM SUBDOMAIN) " -ForegroundColor Green
    Write-Host "=================================================================" -ForegroundColor Cyan
    Write-Host "Đang yêu cầu định danh cố định https://velopass.serveo.net -> http://127.0.0.1:5000 ..."
    ssh -T -i $TunnelKey -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -R velopass:80:127.0.0.1:5000 serveo.net
    exit 0
}

$CertPath = Join-Path $HOME ".cloudflared\cert.pem"
if (-not (Test-Path $CertPath)) {
    Write-Host "[Bước 1/4] Mở trình duyệt đăng nhập Cloudflare và chọn tên miền của bạn..." -ForegroundColor Yellow
    & $Cloudflared tunnel login
}

Write-Host "[Bước 2/4] Khởi tạo Named Tunnel 'velopass'..." -ForegroundColor Cyan
$existing = & $Cloudflared tunnel list | Select-String "\bvelopass\b"
if (-not $existing) {
    & $Cloudflared tunnel create velopass
}

Write-Host "[Bước 3/4] Tự động trỏ bản ghi DNS HTTPS '$Hostname' vào tunnel 'velopass'..." -ForegroundColor Cyan
& $Cloudflared tunnel route dns --overwrite-dns velopass $Hostname

$ConfigDir = Join-Path $HOME ".cloudflared"
$CredFile = Get-ChildItem -Path $ConfigDir -Filter "*.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$ConfigFile = Join-Path $ConfigDir "velopass-config.yml"

@"
tunnel: velopass
credentials-file: $($CredFile.FullName)

ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:5000
  - service: http_status:404
"@ | Set-Content -Encoding UTF8 $ConfigFile

Write-Host "[Bước 4/4] Đang kích hoạt https://$Hostname ..." -ForegroundColor Green
& $Cloudflared tunnel --config $ConfigFile --no-autoupdate run velopass
