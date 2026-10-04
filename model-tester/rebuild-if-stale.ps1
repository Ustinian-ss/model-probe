# 注意：本文件含中文，必须保存为 UTF-8 with BOM。
# PowerShell 5.1 在没有 BOM 时会按 GBK 解析，中文全角括号会吃掉后面的引号，直接报语法错误。
param(
    [switch]$Force,    # 不管新旧，一律重新打包
    [switch]$Check,    # 只检查并报告（不打包、不启动）；退出码 10 = 需要重新打包
    [switch]$Launch,   # 打包完（或已是最新时）直接启动 exe
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Exe = Join-Path $ScriptDir "dist\NvidiaModelTester.exe"

# ---------- 哪些文件算「输入」----------
# 源码、内置模型清单、以及打包配置本身。改完这些而没重新打包，exe 就会跑旧代码。
$inputs = @()
foreach ($sub in @("src", "config")) {
    $full = Join-Path $ScriptDir $sub
    if (Test-Path $full) {
        # 排除 __pycache__ / .pyc / .pytest_cache：跑一次测试就会刷新它们，
        # 否则 exe 会永远被判成「旧」，每次双击都要白等两分钟重新打包。
        $inputs += Get-ChildItem $full -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.FullName -notmatch '\\__pycache__\\' -and
                $_.FullName -notmatch '\\\.pytest_cache\\' -and
                $_.Extension -ne '.pyc'
            }
    }
}
foreach ($name in @("requirements.txt")) {
    $full = Join-Path $ScriptDir $name
    if (Test-Path $full) { $inputs += Get-Item $full }
}

$newest = $inputs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$exeTime = if (Test-Path $Exe) { (Get-Item $Exe).LastWriteTime } else { $null }

$stale = $true
$reason = ""
if ($Force) {
    $reason = "指定了 -Force"
} elseif ($null -eq $exeTime) {
    $reason = "exe 还不存在"
} elseif ($newest -and $newest.LastWriteTime -gt $exeTime) {
    $reason = "「$($newest.Name)」比 exe 新（$(($newest.LastWriteTime).ToString('MM-dd HH:mm')) > $($exeTime.ToString('MM-dd HH:mm'))）"
} else {
    $stale = $false
    $reason = "exe 已是最新（$($exeTime.ToString('MM-dd HH:mm'))）"
}

if ($Check) {
    if ($stale) {
        Write-Host "需要重新打包：$reason"
        exit 10
    }
    Write-Host "无需打包：$reason"
    exit 0
}

if ($stale) {
    Write-Host "源码有更新 → 重新打包中（$reason）"
    Write-Host "（首次或大改约 1-3 分钟，请勿关闭窗口）"
    & (Join-Path $ScriptDir "build_exe.ps1") -SkipDeps
    if ($LASTEXITCODE -ne 0) { throw "重新打包失败（退出码 $LASTEXITCODE）" }
} elseif (-not $Quiet) {
    Write-Host "跳过打包：$reason"
}

if ($Launch) {
    if (-not (Test-Path $Exe)) { throw "找不到 exe：$Exe" }
    Start-Process -FilePath $Exe
    if (-not $Quiet) { Write-Host "已启动：$Exe" }
}
