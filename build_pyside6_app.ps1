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

$appVersion = (& $pythonExe -c "from crimson_texture_forge.constants import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $appVersion) {
    throw "Could not determine app version from crimson_texture_forge.constants.APP_VERSION"
}
$oneFileOutputName = "$appName-$appVersion-windows-portable.exe"
$oneDirOutputName = "$appName-$appVersion-windows"

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
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$oneFileOutputName") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$legacyAppName") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$legacyAppName.exe") -Force -ErrorAction SilentlyContinue
    $pyInstallerArgs += "--onefile"
} else {
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$appName.exe") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$oneFileOutputName") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$oneDirOutputName") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $scriptDir "dist\$legacyAppName.exe") -Force -ErrorAction SilentlyContinue
    $pyInstallerArgs += "--onedir"
}

$pyInstallerArgs += "crimson_texture_forge_app.py"

Write-Host "Building $appName in $Mode mode..."
& $pythonExe @pyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($Mode -eq "onefile") {
    $builtExe = Join-Path $scriptDir "dist\$appName.exe"
    $versionedExe = Join-Path $scriptDir "dist\$oneFileOutputName"
    if (-not (Test-Path $builtExe)) {
        throw "Expected build output not found: $builtExe"
    }
    Move-Item -LiteralPath $builtExe -Destination $versionedExe -Force
} else {
    $builtDir = Join-Path $scriptDir "dist\$appName"
    $versionedDir = Join-Path $scriptDir "dist\$oneDirOutputName"
    if (-not (Test-Path $builtDir)) {
        throw "Expected build output not found: $builtDir"
    }
    Move-Item -LiteralPath $builtDir -Destination $versionedDir -Force
}

Write-Host "Build complete."
if ($Mode -eq "onefile") {
    Write-Host "Output file: $scriptDir\\dist\\$oneFileOutputName"
} else {
    Write-Host "Output folder: $scriptDir\\dist\\$oneDirOutputName"
}
