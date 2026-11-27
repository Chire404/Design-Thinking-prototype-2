# ============================================================================
# VELOPASS — ALL-IN-ONE ONE-CLICK LAUNCHER
# Tự động:
#   1. Dọn tiến trình cũ bị treo (nếu có)
#   2. Kiểm tra & chạy Database Migration
#   3. Khởi động máy chủ VeloPass Production WSGI (127.0.0.1:5000)
#   4. Chờ máy chủ sẵn sàng 200 OK (chống tuyệt đối lỗi 502)
#   5. Kết nối đường hầm HTTPS cố định https://velopass.serveousercontent.com
#   6. Tự động mở trình duyệt khi HTTPS thông suốt & tự kết nối lại nếu rớt mạng
# ============================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "VeloPass — Hệ Thống Thẻ Vé Điện Tử & HTTPS Launcher"
$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ToolsDir  = Join-Path $ProjectRoot "tools"
$TunnelKey = Join-Path $ToolsDir "velopass_tunnel_key"
$PublicUrl = "https://velopass.serveousercontent.com"
$LocalUrl  = "http://velopass.localhost:5000"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "   VELOPASS — KHỞI ĐỘNG TỰ ĐỘNG MÁY CHỦ & TÊN MIỀN HTTPS THỰC TẾ     " -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $PythonExe)) {
    Write-Host "[1/5] Đang khởi tạo môi trường ảo Python (.venv)..." -ForegroundColor Yellow
    python -m venv (Join-Path $ProjectRoot ".venv")
    & $PythonExe -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
}

# 1. Dọn dẹp kết nối SSH tunnel cũ bị kẹt để tránh trùng cổng 80 trên Serveo
Write-Host "[1/5] Kiểm tra và làm sạch kết nối đường hầm cũ..." -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "serveo\.net" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2. Đảm bảo Database Migration luôn cập nhật
Write-Host "[2/5] Đồng bộ lược đồ cơ sở dữ liệu (Migrations)..." -ForegroundColor Cyan
& $PythonExe manage.py migrate | Out-Host

# 3. Kiểm tra hoặc khởi động VeloPass WSGI Server trên cổng 5000
$ServerProc = $null
$ServerHealthy = $false
try {
    $chk = Invoke-WebRequest -Uri "http://127.0.0.1:5000/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    if ($chk.StatusCode -eq 200) { $ServerHealthy = $true }
} catch {}

if ($ServerHealthy) {
    Write-Host "[3/5] Máy chủ VeloPass nội bộ (127.0.0.1:5000) đã đang chạy ổn định!" -ForegroundColor Green
} else {
    Write-Host "[3/5] Đang khởi chạy máy chủ VeloPass WSGI (127.0.0.1:5000)..." -ForegroundColor Cyan
    $ServerProc = Start-Process -FilePath $PythonExe -ArgumentList "run.py","--prod" -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $chk = Invoke-WebRequest -Uri "http://127.0.0.1:5000/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($chk.StatusCode -eq 200) {
                $ServerHealthy = $true
                break
            }
        } catch {}
    }
    if (-not $ServerHealthy) {
        Write-Host "[LỖI] Không thể khởi động máy chủ nội bộ tại cổng 5000!" -ForegroundColor Red
        Read-Host "Nhấn Enter để đóng"
        exit 1
    }
    Write-Host "      -> Máy chủ nội bộ đã sẵn sàng (200 OK)!" -ForegroundColor Green
}

# 4. Kiểm tra khóa định danh SSH cho subdomain velopass
if (-not (Test-Path $TunnelKey)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    ssh-keygen -t ed25519 -f $TunnelKey -q -N '""'
}

Write-Host "[4/5] Sẵn sàng mở đường hầm HTTPS định danh VeloPass!" -ForegroundColor Green
Write-Host ""
Write-Host "  +------------------------------------------------------------------+" -ForegroundColor DarkCyan
Write-Host "  |  Đường dẫn HTTPS (Mọi thiết bị): $PublicUrl" -ForegroundColor Yellow
Write-Host "  |  Đường dẫn trên PC hiện tại   : $LocalUrl" -ForegroundColor White
Write-Host "  |  Tài khoản Admin mẫu          : admin / Admin@123456             |" -ForegroundColor Gray
Write-Host "  |  Tài khoản User mẫu           : nguyenvana / User@123456         |" -ForegroundColor Gray
Write-Host "  +------------------------------------------------------------------+" -ForegroundColor DarkCyan
Write-Host "  (Giữ cửa sổ này mở trong suốt quá trình sử dụng. Nhấn Ctrl+C để tắt)" -ForegroundColor DarkGray
Write-Host ""

# Mở trình duyệt tự động ngay khi HTTPS thông suốt
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

# 5. Duy trì đường hầm HTTPS với cơ chế tự động kết nối lại khi mạng chập chờn
try {
    while ($true) {
        Write-Host "[5/5] Đang kết nối $PublicUrl -> http://127.0.0.1:5000 ..." -ForegroundColor Cyan
        & "C:\WINDOWS\System32\OpenSSH\ssh.exe" -T `
            -i $TunnelKey `
            -o StrictHostKeyChecking=accept-new `
            -o ServerAliveInterval=20 `
            -o ServerAliveCountMax=3 `
            -o ExitOnForwardFailure=yes `
            -R velopass:80:127.0.0.1:5000 serveo.net

        Write-Host "[CẢNH BÁO] Đường hầm HTTPS bị ngắt kết nối. Tự động kết nối lại sau 3 giây..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
} finally {
    Remove-Job -Job $BrowserJob -Force -ErrorAction SilentlyContinue
    if ($ServerProc -and -not $ServerProc.HasExited) {
        Stop-Process -Id $ServerProc.Id -Force -ErrorAction SilentlyContinue
    }
}
