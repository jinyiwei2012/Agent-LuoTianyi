# HarmonyOS 工程构建脚本（Windows）
# 用法: scripts\build_harmony.ps1 [assembleHap|clean|tasks]
# 需要: DevEco Studio 安装于默认路径 C:\Program Files\Huawei\DevEco Studio

param(
    [string]$Task = "assembleHap"
)

$ErrorActionPreference = "Stop"

$devecoRoot = "C:\Program Files\Huawei\DevEco Studio"

# 校验环境
foreach ($p in @("$devecoRoot\tools\node\node.exe", "$devecoRoot\tools\hvigor\bin\hvigorw.bat", "$devecoRoot\tools\ohpm\bin\ohpm.bat", "$devecoRoot\jbr")) {
    if (-not (Test-Path $p)) {
        Write-Error "缺少 DevEco Studio 组件: $p（请确认安装路径或调整 devecoRoot）"
        exit 1
    }
}

$env:NODE_HOME = "$devecoRoot\tools\node"
$env:JAVA_HOME = "$devecoRoot\jbr"
$env:DEVECO_SDK_HOME = "$devecoRoot\sdk"
# 无空格路径，规避 hvigor 的 HVIGOR_USER_HOME 空格限制
$env:HVIGOR_USER_HOME = "C:\hvigor-home"
$env:PATH = "$env:NODE_HOME;$env:JAVA_HOME\bin;$env:PATH"

$harmonyRoot = Resolve-Path "$PSScriptRoot\..\harmony"
$hvigorw = "$devecoRoot\tools\hvigor\bin\hvigorw.bat"

# 首次构建默认产出 unsigned HAP；如需签名请在 DevEco Studio 中登录账号后配置 signingConfigs
$taskArg = "$Task --mode module -p product=default -p buildMode=debug --no-daemon"

Write-Host "==> hvigorw $taskArg" -ForegroundColor Cyan
& $hvigorw $taskArg.Split(" ") 2>&1
exit $LASTEXITCODE
