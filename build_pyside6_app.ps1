param(
    [ValidateSet("onedir", "onefile")]
    [string]$Mode = "onefile"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$appName = "CrimsonForgeToolkit"
$legacyAppName = "DDSRebuildApp"
$previousAppName = "CrimsonTextureForge"
$iconPath = Join-Path $PSScriptRoot "assets\crimson_forge_toolkit.ico"
$customHookDir = Join-Path $PSScriptRoot "pyinstaller_hooks"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$stableDistDir = Join-Path $scriptDir "dist"
$stableBuildDir = Join-Path $scriptDir "build"
$pyInstallerDistDir = Join-Path $stableBuildDir "pyinstaller-dist"
$pyInstallerWorkDir = Join-Path $stableBuildDir "pyinstaller-work"
$vgmstreamRuntimeDir = Join-Path $scriptDir ".tools\vgmstream"
$vgmstreamDownloadUrl = "https://github.com/bnnm/vgmstream-builds/raw/master/bin/vgmstream-latest-test-u.zip"

function Remove-PathWithRetries {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,
        [switch]$Recurse,
        [int]$RetryCount = 8,
        [int]$DelayMilliseconds = 400
    )

    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return
    }

    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        try {
            if ($Recurse) {
                Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
            } else {
                Remove-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
            }
            return
        } catch {
            if ($attempt -ge $RetryCount) {
                throw "Failed to remove '$LiteralPath' after $RetryCount attempt(s): $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Move-PathWithRetries {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath,
        [int]$RetryCount = 8,
        [int]$DelayMilliseconds = 400
    )

    if (-not (Test-Path -LiteralPath $SourcePath)) {
        throw "Source path does not exist: $SourcePath"
    }

    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        try {
            Move-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -ge $RetryCount) {
                throw "Failed to move '$SourcePath' to '$DestinationPath' after $RetryCount attempt(s): $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Stop-AppProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$NamePrefixes
    )

    $targets = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $processName = $_.ProcessName
        foreach ($prefix in $NamePrefixes) {
            if ($processName -like "$prefix*") {
                return $true
            }
        }
        return $false
    } | Sort-Object Id -Unique)

    if (-not $targets) {
        return
    }

    Write-Host "Stopping running build targets..."
    foreach ($proc in $targets) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        } catch {
            Write-Warning "Could not stop process $($proc.ProcessName) [$($proc.Id)]: $($_.Exception.Message)"
        }
    }

    foreach ($proc in $targets) {
        try {
            Wait-Process -Id $proc.Id -Timeout 10 -ErrorAction Stop
        } catch {
            if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
                throw "Process '$($proc.ProcessName)' [$($proc.Id)] is still running after stop was requested."
            }
        }
    }
}

function Ensure-VgmstreamRuntime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeDir
    )

    $cliPath = Join-Path $RuntimeDir "vgmstream-cli.exe"
    if (Test-Path -LiteralPath $cliPath) {
        return $RuntimeDir
    }

    $zipPath = Join-Path $env:TEMP "vgmstream-latest-test-u.zip"
    $extractDir = Join-Path $stableBuildDir "vgmstream-extract"

    Write-Host "Downloading vgmstream runtime..."
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    Remove-PathWithRetries -LiteralPath $extractDir -Recurse
    Invoke-WebRequest -Uri $vgmstreamDownloadUrl -OutFile $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

    $runtimeFiles = Get-ChildItem -LiteralPath $extractDir -File | Where-Object {
        $_.Name -eq "vgmstream-cli.exe" -or $_.Extension -ieq ".dll" -or $_.Name -eq "COPYING"
    }
    if (-not $runtimeFiles) {
        throw "Downloaded vgmstream archive did not contain the expected runtime files."
    }

    foreach ($file in $runtimeFiles) {
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $RuntimeDir $file.Name) -Force
    }

    if (-not (Test-Path -LiteralPath $cliPath)) {
        throw "vgmstream runtime download completed, but vgmstream-cli.exe is still missing."
    }

    return $RuntimeDir
}

function Test-OnefileArchiveIntegrity {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe,
        [Parameter(Mandatory = $true)]
        [string]$ExePath
    )

    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "Cannot validate onefile archive because the EXE does not exist: $ExePath"
    }

$validationScript = @'
from pathlib import Path
import sys

from PyInstaller.archive.readers import CArchiveReader

exe_path = Path(sys.argv[1])
archive = CArchiveReader(str(exe_path))
names = sorted(name for name in archive.toc if name)
if not names:
    raise RuntimeError("Embedded onefile archive was empty.")

validated = 0
total = len(names)
binary_suffixes = (".dll", ".pyd", ".exe")
for index, name in enumerate(names, start=1):
    data = archive.extract(name)
    if data is None:
        raise RuntimeError(f"{name} extracted as None")
    if len(data) == 0 and name.lower().endswith(binary_suffixes):
        raise RuntimeError(f"{name} extracted as empty data")
    validated += 1
    if index % 250 == 0 or index == total:
        print(f"Validated {index}/{total} embedded archive members...")

print(f"Validated all {validated} embedded archive members.")
'@

    $validationOutput = $validationScript | & $PythonExe - $ExePath 2>&1
    if ($LASTEXITCODE -ne 0) {
        $details = ($validationOutput | Out-String).Trim()
        if (-not $details) {
            $details = "No validation details were returned."
        }
        throw "Onefile archive validation failed for '$ExePath'. $details"
    }

    if ($validationOutput) {
        Write-Host ($validationOutput | Out-String).Trim()
    }
}

function Invoke-PyInstallerBuild {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$pythonExe = Join-Path $scriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "python"
}

$appVersion = (& $pythonExe -c "from crimson_forge_toolkit.constants import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $appVersion) {
    throw "Could not determine app version from crimson_forge_toolkit.constants.APP_VERSION"
}
$oneFileOutputName = "$appName-$appVersion-windows-portable.exe"
$oneDirOutputName = "$appName-$appVersion-windows"

Stop-AppProcesses -NamePrefixes @($appName, $previousAppName, $legacyAppName)

$pyInstallerArgs = @(
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--noupx",
    "--windowed",
    "--distpath",
    $pyInstallerDistDir,
    "--workpath",
    $pyInstallerWorkDir,
    "--name",
    $appName
)

if (Test-Path $customHookDir) {
    $pyInstallerArgs += @("--additional-hooks-dir", $customHookDir)
}

if (Test-Path $iconPath) {
    $pyInstallerArgs += @("--icon", $iconPath)
    $pyInstallerArgs += @("--add-data", "$iconPath;assets")
    $pngIconPath = Join-Path $PSScriptRoot "assets\crimson_forge_toolkit.png"
    if (Test-Path $pngIconPath) {
        $pyInstallerArgs += @("--add-data", "$pngIconPath;assets")
    }
}

$thirdPartyNoticesPath = Join-Path $scriptDir "THIRD_PARTY_NOTICES.md"
if (Test-Path $thirdPartyNoticesPath) {
    $pyInstallerArgs += @("--add-data", "$thirdPartyNoticesPath;.")
}

$licensePath = Join-Path $scriptDir "LICENSE"
if (Test-Path $licensePath) {
    $pyInstallerArgs += @("--add-data", "$licensePath;.")
}

$crimsonForgeLicensePath = Join-Path $scriptDir "crimson_forge_toolkit\modding\CrimsonForge_MIT_LICENSE.txt"
if (Test-Path $crimsonForgeLicensePath) {
    $pyInstallerArgs += @("--add-data", "$crimsonForgeLicensePath;third_party")
}

$pyInstallerArgs += @(
    "--collect-all", "numpy",
    "--exclude-module", "PIL.AvifImagePlugin",
    "--exclude-module", "PIL._avif"
)

$resolvedVgmstreamRuntimeDir = Ensure-VgmstreamRuntime -RuntimeDir $vgmstreamRuntimeDir
foreach ($runtimeFile in (Get-ChildItem -LiteralPath $resolvedVgmstreamRuntimeDir -File | Sort-Object Name)) {
    if ($runtimeFile.Name -eq "COPYING") {
        $pyInstallerArgs += @("--add-data", "$($runtimeFile.FullName);vgmstream")
        continue
    }
    $pyInstallerArgs += @("--add-binary", "$($runtimeFile.FullName);vgmstream")
}

if ($Mode -eq "onefile") {
    $pyInstallerArgs += "--onefile"
} else {
    $pyInstallerArgs += "--onedir"
}

New-Item -ItemType Directory -Path $stableDistDir -Force | Out-Null
New-Item -ItemType Directory -Path $stableBuildDir -Force | Out-Null
Remove-PathWithRetries -LiteralPath (Join-Path $stableBuildDir $appName) -Recurse
Remove-PathWithRetries -LiteralPath (Join-Path $stableBuildDir $previousAppName) -Recurse
Remove-PathWithRetries -LiteralPath (Join-Path $stableBuildDir $legacyAppName) -Recurse
Remove-PathWithRetries -LiteralPath $pyInstallerDistDir -Recurse
Remove-PathWithRetries -LiteralPath $pyInstallerWorkDir -Recurse

$pyInstallerArgs += "crimson_forge_toolkit_app.py"

Write-Host "Building $appName in $Mode mode..."
Invoke-PyInstallerBuild -PythonExe $pythonExe -Arguments $pyInstallerArgs

if ($Mode -eq "onefile") {
    $candidateOnefileExe = Join-Path $pyInstallerDistDir "$appName.exe"
    try {
        Test-OnefileArchiveIntegrity -PythonExe $pythonExe -ExePath $candidateOnefileExe
    } catch {
        Write-Warning $_.Exception.Message
        Write-Warning "Retrying the onefile build once with a clean PyInstaller work/dist directory."
        Remove-PathWithRetries -LiteralPath $pyInstallerDistDir -Recurse
        Remove-PathWithRetries -LiteralPath $pyInstallerWorkDir -Recurse
        Invoke-PyInstallerBuild -PythonExe $pythonExe -Arguments $pyInstallerArgs
        Test-OnefileArchiveIntegrity -PythonExe $pythonExe -ExePath $candidateOnefileExe
    }
}

if ($Mode -eq "onefile") {
    $builtExe = Join-Path $pyInstallerDistDir "$appName.exe"
    $versionedExe = Join-Path $stableDistDir $oneFileOutputName
    if (-not (Test-Path $builtExe)) {
        throw "Expected build output not found: $builtExe"
    }
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$appName") -Recurse
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$appName.exe")
    Remove-PathWithRetries -LiteralPath $versionedExe
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$previousAppName") -Recurse
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$previousAppName.exe")
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$previousAppName-$appVersion-windows-portable.exe")
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$legacyAppName") -Recurse
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$legacyAppName.exe")
    Move-PathWithRetries -SourcePath $builtExe -DestinationPath $versionedExe
} else {
    $builtDir = Join-Path $pyInstallerDistDir $appName
    $versionedDir = Join-Path $stableDistDir $oneDirOutputName
    if (-not (Test-Path $builtDir)) {
        throw "Expected build output not found: $builtDir"
    }
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$appName.exe")
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir $oneFileOutputName)
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$previousAppName.exe")
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$previousAppName-$appVersion-windows-portable.exe")
    Remove-PathWithRetries -LiteralPath (Join-Path $stableDistDir "$legacyAppName.exe")
    Remove-PathWithRetries -LiteralPath $versionedDir -Recurse
    Move-PathWithRetries -SourcePath $builtDir -DestinationPath $versionedDir
}

Write-Host "Build complete."
if ($Mode -eq "onefile") {
    Write-Host "Output file: $scriptDir\\dist\\$oneFileOutputName"
} else {
    Write-Host "Output folder: $scriptDir\\dist\\$oneDirOutputName"
}
