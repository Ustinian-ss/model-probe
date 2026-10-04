# 注意：本文件含中文，必须保存为 UTF-8 with BOM。
# PowerShell 5.1 在没有 BOM 时会按 GBK 解析，中文全角括号会吃掉后面的引号，直接报语法错误。
param(
    [switch]$SkipDeps   # 跳过 pip install（离线重打包用；依赖没变时不需要再跑）
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment not found: $VenvPython. Run: python -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

# 相对路径（src/main.py、--add-data config）是相对「当前目录」解析的：
# 以前必须站在 model-tester 目录里执行，这里自己切过去，从哪儿调用都对。
Push-Location $ScriptDir
try {
    if (-not $SkipDeps) {
        & $VenvPython -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "pip install 失败（退出码 $LASTEXITCODE）" }
    }

    $built = Join-Path $ScriptDir "dist\NvidiaModelTester.exe"
    $before = if (Test-Path $built) { (Get-Item $built).LastWriteTime } else { $null }

    # --add-data 必须带上 config/models.json，否则打包后的 exe 内置模型清单为空
    & $VenvPython -m PyInstaller --noconfirm --onefile --windowed --name "NvidiaModelTester" --add-data "config;config" src/main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）" }

    if (-not (Test-Path $built)) { throw "没找到打包产物：$built" }

    # PyInstaller 命中增量缓存时不会重写产物（内容没变就不动文件）。
    # 内容确实与当前源码一致，所以把时间戳对齐到现在 —— 否则「源码比 exe 新」
    # 会一直成立，每次双击都要白等一次打包。
    $rewritten = ($null -eq $before) -or ((Get-Item $built).LastWriteTime -ne $before)
    if (-not $rewritten) { (Get-Item $built).LastWriteTime = Get-Date }

    # 顶层那份才是大家双击用的：以前要手动复制，忘了就会一直跑旧代码（而且复制失败还不报错）。
    $top = Join-Path $ScriptDir "NvidiaModelTester.exe"
    $copied = $false
    foreach ($attempt in 1..5) {
        try {
            Copy-Item $built $top -Force -ErrorAction Stop
            $copied = $true
            break
        } catch {
            # 刚写完的 50MB exe 偶尔被杀软/索引临时占用，退避重试几次
            Start-Sleep -Milliseconds 800
        }
    }
    if (-not $copied) {
        throw "复制到顶层失败（exe 可能正在运行，请先关掉模型测试器）：$top"
    }

    $size = [math]::Round((Get-Item $built).Length / 1MB, 1)
    $stamp = (Get-Item $built).LastWriteTime.ToString("MM-dd HH:mm")
    if ($rewritten) {
        Write-Host "Built: dist\NvidiaModelTester.exe（$stamp，${size}MB）"
    } else {
        Write-Host "内容无变化：复用上次产物（$stamp，${size}MB）"
    }
    Write-Host "已同步覆盖顶层 NvidiaModelTester.exe —— 双击即最新代码"
} finally {
    Pop-Location
}
