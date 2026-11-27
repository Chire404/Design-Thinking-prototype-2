# ============================================================================
# VELOPASS PROTOTYPE 2 — ALL-IN-ONE ONE-CLICK LAUNCHER (PORT 5002)
# Tự động:
#   1. Dọn dẹp mọi tiến trình VeloPass/SSH cũ đang chạy ngầm (chống hiển thị bản cũ)
#   2. Kiểm tra & chạy Database Migration (001 + 002 + 003)
#   3. Khởi động máy chủ VeloPass Prototype 2 trên cổng riêng 127.0.0.1:5002
#   4. Chờ máy chủ sẵn sàng 200 OK (chống lỗi 502)
#   5. Mở đường hầm HTTPS https://velopass.serveousercontent.com -> 127.0.0.1:5002
# ============================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "VeloPass Prototype 2 — Launcher (Port 5002)"
$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$AppPort   = 5002
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ToolsDir  = Join-Path $ProjectRoot "tools"
$TunnelKey = Join-Path $ToolsDir "velopass_tunnel_key"
$PublicUrl = "https://velopass.serveousercontent.com"
$LocalUrl  = "http://velopass.localhost:$AppPort"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "   VELOPASS PROTOTYPE 2 — KHỞI ĐỘNG TỰ ĐỘNG & TÊN MIỀN HTTPS          " -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Dọn sạch tiến trình SSH tunnel cũ và mọi tiến trình run.py cũ (kể cả Prototype 1)
Write-Host "[1/5] Dọn dẹp tiến trình máy chủ / đường hầm cũ..." -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "serveo\.net" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "run\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Milliseconds 600

# 2. Chạy Database Migrations
Write-Host "[2/5] Đồng bộ lược đồ cơ sở dữ liệu Prototype 2 (Migrations)..." -ForegroundColor Cyan
$env:APP_PORT = "$AppPort"
& $PythonExe manage.py migrate | Out-Host

# 3. Khởi động mới máy chủ VeloPass Prototype 2 trên cổng 5002
Write-Host "[3/5] Đang khởi chạy máy chủ VeloPass Prototype 2 (127.0.0.1:$AppPort)..." -ForegroundColor Cyan
$ServerProc = Start-Process -FilePath $PythonExe -ArgumentList "run.py","--prod" -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden
$ServerHealthy = $false
for ($i = 1; $i -le 24; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $chk = Invoke-WebRequest -Uri "http://127.0.0.1:$AppPort/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($chk.StatusCode -eq 200) {
            $ServerHealthy = $true
            break
        }
    } catch {}
}
if (-not $ServerHealthy) {
    Write-Host "[LỖI] Không thể khởi động máy chủ nội bộ tại cổng $AppPort!" -ForegroundColor Red
    Read-Host "Nhấn Enter để đóng"
    exit 1
}
Write-Host "      -> Máy chủ Prototype 2 đã sẵn sàng tại cổng $AppPort (200 OK)!" -ForegroundColor Green

# 4. Kiểm tra khóa định danh SSH
if (-not (Test-Path $TunnelKey)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    ssh-keygen -t ed25519 -f $TunnelKey -q -N '""'
}

Write-Host "[4/5] Đang mở đường hầm HTTPS..." -ForegroundColor Green
Write-Host ""
Write-Host "  +------------------------------------------------------------------+" -ForegroundColor DarkCyan
Write-Host "  |  Đường dẫn HTTPS (Mọi thiết bị): $PublicUrl" -ForegroundColor Yellow
Write-Host "  |  Đường dẫn trên PC hiện tại   : $LocalUrl" -ForegroundColor White
Write-Host "  |  Tài khoản Admin mẫu          : admin / Admin@123456             |" -ForegroundColor Gray
Write-Host "  |  Tài khoản User mẫu           : nguyenvana / User@123456         |" -ForegroundColor Gray
Write-Host "  +------------------------------------------------------------------+" -ForegroundColor DarkCyan
Write-Host ""

$BrowserJob = Start-Job -ScriptBlock {
    param($Url, $FallbackUrl)
    for ($k = 1; $k -le 15; $k++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                Start-Process $Url
                return
            }
        } catch {}
    }
    Start-Process $FallbackUrl
} -ArgumentList $PublicUrl, $LocalUrl

try {
    while ($true) {
        Write-Host "[5/5] Đang kết nối $PublicUrl -> http://127.0.0.1:$AppPort ..." -ForegroundColor Cyan
        & "C:\WINDOWS\System32\OpenSSH\ssh.exe" -T `
            -i $TunnelKey `
            -o StrictHostKeyChecking=accept-new `
            -o ServerAliveInterval=20 `
            -o ServerAliveCountMax=3 `
            -o ExitOnForwardFailure=yes `
            -R "velopass:80:127.0.0.1:$AppPort" serveo.net

        Write-Host "[CẢNH BÁO] Đường hầm HTTPS bị ngắt kết nối. Kết nối lại sau 3 giây..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
} finally {
    Remove-Job -Job $BrowserJob -Force -ErrorAction SilentlyContinue
    if ($ServerProc -and -not $ServerProc.HasExited) {
        Stop-Process -Id $ServerProc.Id -Force -ErrorAction SilentlyContinue
    }
}
