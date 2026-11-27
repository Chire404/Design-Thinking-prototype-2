# ============================================================================
# Tạo Shortcut "VeloPass HTTPS" trên màn hình Desktop Windows
# ============================================================================
$ErrorActionPreference = "Stop"
$ProjectRoot  = Split-Path -Parent $PSScriptRoot
$LauncherPs1  = Join-Path $PSScriptRoot "launch_velopass_all.ps1"
$IconPath     = Join-Path $ProjectRoot "frontend\static\icons\velopass.ico"

$DesktopDir = [Environment]::GetFolderPath("Desktop")
if (-not $DesktopDir -or -not (Test-Path $DesktopDir)) {
    $Candidates = @(
        (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -Name "Desktop" -ErrorAction SilentlyContinue).Desktop,
        (Join-Path $env:USERPROFILE "OneDrive\Desktop"),
        (Join-Path $env:USERPROFILE "Desktop")
    )
    foreach ($c in $Candidates) {
        if ($c) {
            $expanded = [Environment]::ExpandEnvironmentVariables($c)
            if (Test-Path $expanded) {
                $DesktopDir = $expanded
                break
            }
        }
    }
}

$ShortcutPath = Join-Path $DesktopDir "VeloPass HTTPS.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Shortcut.Arguments = "-ExecutionPolicy Bypass -File `"$LauncherPs1`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Description = "Khởi động tự động máy chủ VeloPass và tên miền HTTPS thực tế"
$Shortcut.Save()

Write-Output "SHORTCUT_CREATED=$ShortcutPath"
