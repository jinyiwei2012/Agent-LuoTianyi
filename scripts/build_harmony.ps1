# HarmonyOS project build script (Windows)
# Usage: scripts\build_harmony.ps1 [assembleHap|clean|tasks]
# Requires: DevEco Studio installed at default path C:\Program Files\Huawei\DevEco Studio

param(
    [string]$Task = "assembleHap"
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

# First build outputs unsigned HAP; configure signingConfigs in DevEco Studio for signed builds
$taskArg = "$Task --mode module -p product=default -p buildMode=debug --no-daemon"

Write-Host "==> hvigorw $taskArg" -ForegroundColor Cyan
& $hvigorw $taskArg.Split(" ") 2>&1
exit $LASTEXITCODE