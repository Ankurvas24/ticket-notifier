# PowerShell git push helper (Windows / cross-platform PowerShell).
# Runs from the repo root regardless of where it was invoked from.

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Pick git from PATH, or fall back to the standard Windows install path.
$git = "git"
if (-not (Get-Command $git -ErrorAction SilentlyContinue)) {
    $fallback = "C:\Program Files\Git\cmd\git.exe"
    if (Test-Path $fallback) { $git = $fallback }
    else {
        Write-Error "git not found on PATH and not at $fallback"
        exit 1
    }
}

# Clean up stale lock files from previous interrupted pushes
$locks = @(
    ".git\index.lock",
    ".git\HEAD.lock",
    ".git\config.lock",
    ".git\refs\heads\main.lock",
    ".git\objects\maintenance.lock"
)
foreach ($f in $locks) {
    if (Test-Path $f) { Remove-Item -Force $f }
}

$msg = if ($args.Count -gt 0) { $args[0] } else {
    "chore: cross-system hardening + stealth + cart URL fallbacks"
}

& $git add -A                       *>> git_output.log
& $git commit -m $msg               *>> git_output.log
& $git pull --rebase origin main    *>> git_output.log
& $git push origin main             *>> git_output.log
"DONE"                              *>> git_output.log
