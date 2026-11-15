# install.ps1 — code_map plugin installer for Windows
# Installs code_map into both Hermes plugin directories and creates profile symlinks.
#
# Usage (PowerShell):
#   .\install.ps1 [-Profile <name>] [-AllProfiles] [-DryRun]
#
# Dual install paths (Windows):
#   1. C:\Users\<user>\AppData\Local\hermes\plugins\code_map\
#   2. $HOME\.hermes\plugins\code_map\
#
# Profile symlinks use mklink /D (Windows directory junction/symlink).
param(
    [string]$Profile = "",
    [switch]$AllProfiles,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$PluginName = "code_map"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== code_map installer (Windows) ===" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Install to both plugin directories ────────────────────────────────
$ProfilesDirBase = "C:\Users\$env:USERNAME\AppData\Local\hermes"
$HermesHome = Join-Path $env:USERPROFILE ".hermes"
$WinPlugins = Join-Path $ProfilesDirBase "plugins"
$HomePlugins = Join-Path $HermesHome "plugins"

$InstallTargets = @(
    @{ Path = $WinPlugins; Label = "AppData (Windows native)" },
    @{ Path = $HomePlugins; Label = "$HOME\.hermes" }
)

Write-Host "[1/3] Installing plugin source..." -ForegroundColor Yellow
foreach ($t in $InstallTargets) {
    $dest = Join-Path $t.Path $PluginName
    if ($DryRun) {
        Write-Host "  [dry-run] would install to $($t.Label): $dest"
    } else {
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        # Copy all files except .git (keep existing .git if present)
        Get-ChildItem $ScriptDir -Exclude '.git' -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($ScriptDir.Length).Replace('\', '/')
            $targetFile = Join-Path $dest $rel
            $targetDir = Split-Path $targetFile -Parent
            New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
            Copy-Item $_.FullName -Destination $targetFile -Force
        }
        # Copy .git if not exists
        if (-not (Test-Path (Join-Path $dest '.git'))) {
            if (Test-Path (Join-Path $ScriptDir '.git')) {
                Copy-Item -Path (Join-Path $ScriptDir '.git') -Destination (Join-Path $dest '.git') -Recurse -Force
            }
        }
        Write-Host "  [ok] $($t.Label): $dest"
    }
}

# ── Step 2: Profile symlinks ─────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/3] Setting up profile symlinks..." -ForegroundColor Yellow

$AllProfiles = @()
if ($Profile) {
    $AllProfiles = @($Profile)
} elseif ($AllProfiles.IsPresent) {
    $AllProfiles = Get-ChildItem "$ProfilesDirBase\profiles" -Directory | Where-Object { $_.Name -notmatch '^\.' } | ForEach-Object { $_.Name }
} else {
    $AllProfiles = Get-ChildItem "$ProfilesDirBase\profiles" -Directory | Where-Object { $_.Name -notmatch '^\.' } | ForEach-Object { $_.Name }
}

foreach ($prof in $AllProfiles) {
    $cfgPath = Join-Path "$ProfilesDirBase\profiles\$prof" "config.yaml"
    $hasEnabled = $false
    if (Test-Path $cfgPath) {
        $cfgContent = Get-Content $cfgPath -Raw
        if ($cfgContent -match '-\s*code_map\s*$') {
            $hasEnabled = $true
        }
    }

    if (-not $hasEnabled) { continue }

    $pluginsDir = Join-Path "$ProfilesDirBase\profiles\$prof" "plugins"
    $linkTarget = Join-Path $pluginsDir $PluginName
    $srcPath = Join-Path $WinPlugins $PluginName  # prefer AppData as primary source

    if ($DryRun) {
        Write-Host "  [dry-run] would create symlink: $linkTarget -> $srcPath"
    } else {
        # Clean up existing
        if (Test-Path $linkTarget) {
            if ((Get-Item $linkTarget).LinkType -eq "Symlink") {
                Remove-Item $linkTarget -Force
            } elseif ((Get-Item $linkTarget).PSIsContainer) {
                Remove-Item $linkTarget -Recurse -Force
            } else {
                Remove-Item $linkTarget -Force
            }
        }
        # Create symlink (Windows native)
        cmd /c "mklink /D `"$linkTarget`" `"$srcPath`"" | Out-Null
        Write-Host "  [ok] $prof/plugins/$PluginName -> $srcPath"
    }
}

# ── Step 3: Verify ───────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/3] Verification..." -ForegroundColor Yellow
$installed = 0
foreach ($t in $InstallTargets) {
    $check = Join-Path $t.Path "$PluginName/__init__.py"
    if (Test-Path $check) { $installed++ }
}
Write-Host "  installed to $installed location(s)"
Write-Host ""
Write-Host "Done. Restart the Hermes gateway for changes to take effect." -ForegroundColor Green
