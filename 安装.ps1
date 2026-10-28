# 元力2代远程控制包 —— 一键安装控制端
# 用法：右键本文件 → 使用 PowerShell 运行
# 它会把「控制包」复制到 用户目录\meta2-controller，建好 venv，装好 bleak。

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
$target = Join-Path $env:USERPROFILE "meta2-controller"

Write-Host ""
Write-Host "安装位置: $target" -ForegroundColor DarkGray
Write-Host ""

# ---------- 1) 找 Python ----------
Write-Host "== 1/4 找 Python 3.10+ ==" -ForegroundColor Cyan
$launcher = $null

# 1a) PATH 里的启动器
foreach ($cand in @("py", "python", "python3")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $ver = & $cand --version 2>&1
    if ($LASTEXITCODE -ne 0) { continue }
    if ("$ver" -match "3\.(\d+)") {
        if ([int]$matches[1] -ge 10) { $launcher = $cmd.Source; break }
    }
}

# 1b) PATH 里没有（商店版 python 是空壳），扫常见安装位置
if (-not $launcher) {
    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:APPDATA\uv\python\cpython-3*\python.exe",
        "C:\Python3*\python.exe",
        "C:\Program Files\Python3*\python.exe"
    )
    $found = @()
    foreach ($pat in $patterns) {
        $found += @(Get-ChildItem $pat -ErrorAction SilentlyContinue)
    }
    foreach ($exe in ($found | Sort-Object FullName -Descending)) {
        $ver = & $exe.FullName --version 2>&1
        if ($LASTEXITCODE -ne 0) { continue }
        if ("$ver" -match "3\.(\d+)") {
            if ([int]$matches[1] -ge 10) { $launcher = $exe.FullName; break }
        }
    }
}

if (-not $launcher) {
    Write-Host "[X] 没找到 Python 3.10+。" -ForegroundColor Red
    Write-Host "    去 python.org 装一个，安装时勾选 Add Python to PATH，然后再跑一次。" -ForegroundColor Red
    Write-Host "    用 uv 的话：先 uv python install 3.11，再跑一次本脚本。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "    用 $launcher（$(& $launcher --version 2>&1)）" -ForegroundColor Green

# ---------- 2) 复制控制包 ----------
Write-Host "== 2/4 复制控制包 ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item (Join-Path $here "控制包\*") $target -Recurse -Force
Write-Host "    已复制到 $target" -ForegroundColor Green

# ---------- 3) 建 venv ----------
Write-Host "== 3/4 建虚拟环境 ==" -ForegroundColor Cyan
$venvDir = Join-Path $target ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $launcher -m venv $venvDir
}
if (-not (Test-Path $venvPy)) {
    Write-Host "[X] 虚拟环境建不起来。试试用管理员 PowerShell 再跑一次。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "    venv: $venvPy" -ForegroundColor Green

# ---------- 4) 装依赖 ----------
Write-Host "== 4/4 安装 bleak ==" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $target "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] pip 安装失败。检查网络，或换 pip 镜像：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple bleak" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "== 验证 ==" -ForegroundColor Cyan
& $venvPy -c "import importlib.metadata as m; print('  bleak', m.version('bleak'))"
& $venvPy (Join-Path $target "presets.py") --list

Write-Host ""
Write-Host "[OK] 安装完成" -ForegroundColor Green
Write-Host ""
Write-Host "下一步：" -ForegroundColor Yellow
Write-Host "  1) 把 插件\astrbot_plugin_meta2 复制到 AstrBot 的 data\plugins\，重载插件"
Write-Host "  2) AstrBot 后台 -> 插件 -> 元力2代远程控制 -> allowed_qq 填你的 QQ 号"
Write-Host "  3) 设备开机，手机 App 断开连接"
Write-Host "  4) 想先手动验证：cd $target 然后 .\run-meta2.cmd --scan"
Write-Host ""
Read-Host "按回车关闭"
