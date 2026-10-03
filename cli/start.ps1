<#
.SYNOPSIS
    Fengcode 一键启动（PowerShell 版）

.DESCRIPTION
    自动查找 Python、安装依赖、启动服务。
    支持 -Port 指定端口、-Cli 进命令行、-Doctor 自检、-NoBrowser 不开浏览器。

    本文件必须以 UTF-8 **带 BOM** 保存：PowerShell 5.1 读无 BOM 的 UTF-8
    会按系统 ANSI(GBK) 解析，中文会破坏语法导致脚本无法运行。

.EXAMPLE
    .\start.ps1                 # 启动服务并打开浏览器
    .\start.ps1 -Port 9000      # 指定端口
    .\start.ps1 -Cli            # 进入命令行对话
    .\start.ps1 -Doctor         # 只做环境自检
    .\start.ps1 -Install        # 只装依赖
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$BindHost = "",
    [switch]$Cli,
    [switch]$Doctor,
    [switch]$Install,
    [switch]$NoBrowser,
    [switch]$NoMcp,
    [switch]$Quiet
)

# ---------- 编码 ----------
$script:Utf8Ok = $false
try {
    $null = chcp 65001
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
    $script:Utf8Ok = $true
} catch {
    $script:Utf8Ok = $false
}
if ($script:Utf8Ok) {
    try {
        $probe = [char]0x4E2D + [char]0x6587
        $back = [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::UTF8.GetBytes($probe))
        if ($back -ne $probe) { $script:Utf8Ok = $false }
    } catch {
        $script:Utf8Ok = $false
    }
}
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# ---------- 输出（编码不可用时降级为英文，避免乱码）----------
function Say {
    param([string]$Zh = "", [string]$En = "", [string]$Color = "")
    $msg = if ($script:Utf8Ok) { $Zh } elseif ($En) { $En } else { $Zh }
    if ($Color) { Write-Host $msg -ForegroundColor $Color } else { Write-Host $msg }
}
function SayTag {
    param([string]$Tag, [string]$Zh, [string]$En, [string]$Color)
    Write-Host "  " -NoNewline
    Write-Host "$Tag " -ForegroundColor $Color -NoNewline
    Say $Zh $En
}
function SayOk   { param([string]$Zh, [string]$En = "")
    if ($script:Utf8Ok) { SayTag "[OK]" $Zh "" "Green" } else { SayTag "[OK]" $En $En "Green" } }
function SayWarn { param([string]$Zh, [string]$En = "")
    if ($script:Utf8Ok) { SayTag "[!]" $Zh "" "Yellow" } else { SayTag "[!]" $En $En "Yellow" } }
function SayErr  { param([string]$Zh, [string]$En = "")
    if ($script:Utf8Ok) { SayTag "[X]" $Zh "" "Red" } else { SayTag "[X]" $En $En "Red" } }
function SayStep { param([int]$N, [string]$Zh, [string]$En = "")
    Say "  [$N/4] $Zh" "  [$N/4] $En" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Say ""
Say "  ========================================================" "" "Cyan"
Say "    Fengcode  -  AI Agent for Windows" "" "Cyan"
Say "  ========================================================" "" "Cyan"
Say ""

# ---------- 1. Python ----------
SayStep 1 "查找 Python" "Looking for Python"

function Test-PyOk {
    param([string]$Exe)
    if ([string]::IsNullOrEmpty($Exe)) { return $false }
    if (-not (Test-Path -LiteralPath $Exe -ErrorAction SilentlyContinue)) { return $false }
    $outText = ""
    try { $outText = (& $Exe --version 2>&1 | Out-String) } catch { return $false }
    if ($outText -notmatch "Python 3") { return $false }
    if ($outText -match "Python 3\.(\d+)") {
        $cap = $Matches
        $minor = [int]($cap[1])
        if ($minor -ge 10) { return $true }
    }
    return $false
}

function Find-Python {
    foreach ($n in @("python", "python3")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($null -ne $c) {
            if (Test-PyOk -Exe $c.Source) { return $c.Source }
        }
    }
    $la = $env:LOCALAPPDATA
    $pf = $env:ProgramFiles
    $fixed = @()
    foreach ($v in @("313", "312", "311", "310")) {
        $fixed += (Join-Path $la ("Programs\Python\Python" + $v + "\python.exe"))
        $fixed += (Join-Path $pf ("Python" + $v + "\python.exe"))
    }
    $fixed += "C:\Python312\python.exe"
    $fixed += "D:\Python312\python.exe"
    foreach ($f in $fixed) {
        if (Test-PyOk -Exe $f) { return $f }
    }
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyCmd) { return "py -3" }
    return $null
}

$Py = Find-Python
if ($null -eq $Py -or $Py -eq "") {
    SayErr "没有找到 Python 3.10 或更高版本" "Python 3.10+ not found"
    Say ""
    Say "  请先安装：https://www.python.org/downloads/" "  Install from: https://www.python.org/downloads/" "Gray"
    Say "  安装时记得勾选 [Add Python to PATH]" "  Remember to check [Add Python to PATH]" "Gray"
    Say ""
    Read-Host "  按回车退出 / Press Enter to exit" | Out-Null
    exit 1
}

$PyVer = "?"
try {
    $verOut = (& $Py --version 2>&1 | Out-String)
    if ($verOut) { $PyVer = $verOut.Trim() }
} catch { }
SayOk "$Py  ($PyVer)" "$Py  ($PyVer)"

# 统一调用（$Py 可能是 "py -3"）
function Invoke-Py {
    if ($Py -eq "py -3") { & py -3 @args } else { & $Py @args }
}

# ---------- 2. 依赖 ----------
SayStep 2 "检查依赖" "Checking dependencies"
$hasFeng = $false
try {
    Invoke-Py -c "import fengcode" 2>&1 | Out-Null
    $hasFeng = ($LASTEXITCODE -eq 0)
} catch { $hasFeng = $false }

if (-not $hasFeng) {
    Say "        首次运行，安装依赖中（1-3 分钟，请稍候）..." "        First run: installing deps (1-3 min)..." "Gray"
    Invoke-Py -m pip install --disable-pip-version-check -q -e . 2>&1 | Out-Null
    try {
        Invoke-Py -c "import fengcode" 2>&1 | Out-Null
        $hasFeng = ($LASTEXITCODE -eq 0)
    } catch { $hasFeng = $false }
    if (-not $hasFeng) {
        SayWarn "直连失败，改用清华镜像重试..." "Retrying with Tsinghua mirror..."
        Invoke-Py -m pip install --disable-pip-version-check -q -e . -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | Out-Null
        try {
            Invoke-Py -c "import fengcode" 2>&1 | Out-Null
            $hasFeng = ($LASTEXITCODE -eq 0)
        } catch { $hasFeng = $false }
    }
    if (-not $hasFeng) {
        SayErr "依赖安装失败" "Dependency install failed"
        Say "        请手动执行： $Py -m pip install -e ." "        Run manually: $Py -m pip install -e ." "Gray"
        Read-Host "  按回车退出 / Press Enter" | Out-Null
        exit 1
    }
}
SayOk "依赖已就绪" "Dependencies OK"

if ($Install) {
    Say ""
    SayOk "安装完成，可运行 start.ps1 启动" "Install finished. Run start.ps1 to launch"
    exit 0
}

# ---------- 3. 模型配置 ----------
SayStep 3 "检查模型配置" "Checking model config"
$hasKey = $false
try {
    Invoke-Py -c "import sys;from fengcode.config import get_manager as g;m=g();sys.exit(0 if any(m.resolve_api_key(p) for p in m.config.providers) else 1)" 2>&1 | Out-Null
    $hasKey = ($LASTEXITCODE -eq 0)
} catch { $hasKey = $false }

if ($hasKey) {
    SayOk "已配置可用模型" "Model configured"
} else {
    SayWarn "还没有配置模型" "No model configured yet"
    Say ""
    Say "  --------------------------------------------------------" "" "Yellow"
    Say "    首次使用提示 / First-run note" "" "Yellow"
    Say "  --------------------------------------------------------" "" "Yellow"
    Say "    服务启动后会自动打开浏览器，请到：" "  After launch, open in browser:" "Gray"
    Say "      设置 -> 模型供应商 -> 添加供应商" "    Settings -> Providers -> Add" "White"
    Say "    或点 [从环境变量导入] 把已有配置搬过来。" "    Or click [Import from env vars]." "Gray"
    Say "  --------------------------------------------------------" "" "Yellow"
    Say ""
}

if ($Doctor) {
    Say ""
    Invoke-Py -m fengcode.cli.main doctor
    exit $LASTEXITCODE
}

# ---------- 4. 启动 ----------
SayStep 4 "启动" "Launching"

if ($Cli) {
    Say ""
    Say "  进入命令行对话（Ctrl+Q 退出 / F1 帮助）" "  Entering CLI (Ctrl+Q quit / F1 help)" "Gray"
    Say ""
    Invoke-Py -m fengcode.cli.main chat
    exit $LASTEXITCODE
}

$serveArgs = @()
if ($Port -gt 0) { $serveArgs += @("--port", "$Port") }
if ($BindHost -ne "") { $serveArgs += @("--host", $BindHost) }
if (-not $NoBrowser) { $serveArgs += "--open" }
if ($NoMcp) { $serveArgs += "--no-mcp" }

Say ""
Say "  服务启动中，浏览器会自动打开。" "  Starting service, browser will open." "Gray"
Say "  关闭此窗口即可停止服务（或按 Ctrl+C）。" "  Close this window to stop (or Ctrl+C)." "Gray"
Say ""

Invoke-Py -m fengcode.cli.main serve @serveArgs
$code = $LASTEXITCODE

if ($code -ne 0) {
    Say ""
    SayErr "服务异常退出（退出码 $code）" "Service exited with code $code"
    Say ""
    Say "  常见原因：" "  Common causes:" "Gray"
    Say "    1. 端口被占用 -> 换端口：.\start.ps1 -Port 9000" "    1. Port in use -> .\start.ps1 -Port 9000" "Gray"
    Say "    2. 依赖缺失   -> $Py -m pip install -e ." "    2. Missing deps -> $Py -m pip install -e ." "Gray"
    Say "    3. 配置损坏   -> 删除数据目录下的 config.toml" "    3. Bad config -> delete config.toml" "Gray"
    Say ""
    Say "  完整自检：.\start.ps1 -Doctor" "  Full check: .\start.ps1 -Doctor" "Gray"
    Say ""
    Read-Host "  按回车退出 / Press Enter to exit" | Out-Null
}
exit $code
