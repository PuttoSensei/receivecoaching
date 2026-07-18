# Run this ONCE in PowerShell to install a `coach` alias globally on your machine.
# After that, you can type `coach` from any terminal and it opens the app here.
#
# Usage:
#   1. Open PowerShell
#   2. cd C:\Heya\receivecoaching
#   3. .\install-alias.ps1
#
# If you get a script execution error, run this first:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $scriptDir "coach.bat"

if (-not (Test-Path $batPath)) {
    Write-Error "coach.bat not found at $batPath"
    exit 1
}

$profilePath = $PROFILE

# Ensure the profile file exists
if (-not (Test-Path $profilePath)) {
    New-Item -ItemType File -Path $profilePath -Force | Out-Null
    Write-Host "Created PowerShell profile at $profilePath"
}

$aliasLine = "function coach { & `"$batPath`" @args }"
$existing = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue

# Match an actual `function coach {` definition, not any substring (a
# `function coaching` or a comment mentioning it should not block install).
if ($existing -and $existing -match '(?m)^\s*function\s+coach\s*\{') {
    Write-Host "The 'coach' function is already defined in your profile. No changes made."
    Write-Host "Profile: $profilePath"
} else {
    Add-Content -Path $profilePath -Value "`n# Receive Coaching launcher`n$aliasLine`n"
    Write-Host "Added 'coach' function to $profilePath"
    Write-Host ""
    Write-Host "To use immediately in THIS terminal, run:"
    Write-Host "    . `$PROFILE"
    Write-Host ""
    Write-Host "New terminals will have it automatically. Then just type:"
    Write-Host "    coach"
    Write-Host "    coach --coach grief"
    Write-Host "    coach --list-coaches"
}
