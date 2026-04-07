param(
    [ValidateSet("onedir", "onefile")]
    [string]$Mode = "onefile"
)

$ErrorActionPreference = "Stop"
$appName = "CrimsonTextureForge"
$legacyAppName = "DDSRebuildApp"
$iconPath = Join-Path $PSScriptRoot "assets\crimson_texture_forge.ico"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonExe = Join-Path $scriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "python"
}

Get-Process -Name $appName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name $legacyAppName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

$pyInstallerArgs = @(
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name",
    $appName
)

if (Test-Path $iconPath) {
    $pyInstallerArgs += @("--icon", $iconPath)
    $pyInstallerArgs += @("--add-data", "$iconPath;assets")
    $pngIconPath = Join-Path $PSScriptRoot "assets\crimson_texture_forge.png"
    if (Test-Path $pngIconPath) {
        $pyInstallerArgs += @("--add-data", "$pngIconPath;assets")
    }
}

if ($Mode -eq "onefile") {
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$appName") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$appName.exe") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$legacyAppName") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$legacyAppName.exe") -Force -ErrorAction SilentlyContinue
    $pyInstallerArgs += "--onefile"
} else {
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$appName.exe") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$legacyAppName.exe") -Force -ErrorAction SilentlyContinue
    $pyInstallerArgs += "--onedir"
}

$pyInstallerArgs += "crimson_texture_forge_app.py"

Write-Host "Building $appName in $Mode mode..."
& $pythonExe @pyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Build complete."
if ($Mode -eq "onefile") {
    Write-Host "Output file: $scriptDir\\dist\\$appName.exe"
} else {
    Write-Host "Output folder: $scriptDir\\dist\\$appName"
}
