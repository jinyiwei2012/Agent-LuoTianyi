# HarmonyOS project build script (Windows)
# Usage: scripts\build_harmony.ps1 [assembleHap|clean|tasks]
# Requires: DevEco Studio installed at default path C:\Program Files\Huawei\DevEco Studio
# Result detection: read .hvigor/outputs/build-logs/build.log after build;
#   any "BUILD FAILED" | "COMPILE RESULT:FAIL" | "Error Message:" in NEW log lines => exit 1

param(
    [string]$Task = "assembleHap",
    [string]$BuildMode = "debug"
)

$ErrorActionPreference = "Stop"

$devecoRoot = "C:\Program Files\Huawei\DevEco Studio"

# Verify environment
foreach ($p in @("$devecoRoot\tools\node\node.exe", "$devecoRoot\tools\hvigor\bin\hvigorw.bat", "$devecoRoot\tools\ohpm\bin\ohpm.bat", "$devecoRoot\jbr")) {
    if (-not (Test-Path $p)) {
        Write-Error "Missing DevEco Studio component: $p (verify path or adjust devecoRoot)"
        exit 1
    }
}

$env:NODE_HOME = "$devecoRoot\tools\node"
$env:JAVA_HOME = "$devecoRoot\jbr"
$env:DEVECO_SDK_HOME = "$devecoRoot\sdk"
# Space-free path to avoid hvigor HVIGOR_USER_HOME restriction
$env:HVIGOR_USER_HOME = "C:\hvigor-home"
$env:PATH = "$env:NODE_HOME;$env:JAVA_HOME\bin;$env:PATH"

$harmonyRoot = Resolve-Path "$PSScriptRoot\..\harmony"
$hvigorw = "$devecoRoot\tools\hvigor\bin\hvigorw.bat"
$buildLog = "$harmonyRoot\.hvigor\outputs\build-logs\build.log"

# Record existing log line count before build (build.log appends across runs)
$linesBefore = 0
if (Test-Path $buildLog) {
    $linesBefore = (Get-Content -LiteralPath $buildLog | Measure-Object -Line).Lines
}

# First build outputs unsigned HAP; configure signingConfigs in DevEco Studio for signed builds
$taskArg = "$Task --mode module -p product=default -p buildMode=$BuildMode --no-daemon"

Write-Host "==> hvigorw $taskArg" -ForegroundColor Cyan
# Run via cmd /c: keeps hvigor stderr raw (PS 5.1 would wrap it as RemoteException noise)
$outFile = Join-Path $env:TEMP "hvigor_last_output.txt"
if (Test-Path $outFile) { Remove-Item -LiteralPath $outFile -Force }
cmd /c "`"$hvigorw`" $taskArg > `"$outFile`" 2>&1"
$exitCode = $LASTEXITCODE

# Read NEW lines appended by this build
$newLines = @()
if (Test-Path $buildLog) {
    $all = Get-Content -LiteralPath $buildLog
    $newLines = $all | Select-Object -Skip $linesBefore
}

# FAIL detection: newest result markers or compiler errors
$failMatch = $newLines | Select-String -Pattern "BUILD FAILED|COMPILE RESULT:FAIL|Error Message:" | Select-Object -First 1
if ($failMatch) {
    Write-Host "`n==> BUILD FAILED (see $buildLog)" -ForegroundColor Red
    # Print tail of hvigor stdout for quick diagnosis
    if (Test-Path $outFile) {
        Get-Content -LiteralPath $outFile | Select-Object -Last 15 | ForEach-Object { Write-Host $_ }
    }
    exit 1
}

if ($exitCode -ne 0) {
    Write-Host "`n==> hvigor exit code $exitCode (no FAIL marker found in log)" -ForegroundColor Yellow
    exit $exitCode
}

Write-Host "`n==> BUILD OK" -ForegroundColor Green
exit 0