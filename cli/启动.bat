@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title Fengcode

REM ============================================================
REM  Fengcode 一键启动（Windows）
REM  双击即可：自动找 Python、装依赖、启动服务并打开浏览器
REM ============================================================

cd /d "%~dp0"

echo.
echo   ========================================================
echo     Fengcode  ·  中文 AI Agent
echo   ========================================================
echo.

REM ---------- 1. 找 Python ----------
set PY=
for %%P in (python.exe) do (
    if exist "%%~$PATH:P" set PY=python
)
if "%PY%"=="" (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%ProgramFiles%\Python312\python.exe"
        "%ProgramFiles%\Python311\python.exe"
        "C:\Python312\python.exe"
        "D:\Python312\python.exe"
    ) do (
        if exist %%D if "%PY%"=="" set PY=%%~D
    )
)
if "%PY%"=="" (
    where py >nul 2>&1
    if !errorlevel! equ 0 set PY=py -3
)
if "%PY%"=="" (
    echo   [错误] 没有找到 Python。
    echo.
    echo   请先安装 Python 3.10 或更高版本：
    echo     https://www.python.org/downloads/
    echo   安装时记得勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)
echo   [1/4] Python: %PY%
"%PY%" --version 2>&1 | findstr /r "3\.[0-9]*" >nul
if errorlevel 1 (
    "%PY%" --version
)

REM ---------- 2. 检查依赖 ----------
echo   [2/4] 检查依赖...
"%PY%" -c "import fengcode" >nul 2>&1
if errorlevel 1 (
    echo         首次运行，正在安装依赖（约 1-3 分钟，请稍候）...
    "%PY%" -m pip install --disable-pip-version-check -q -e . 2>&1
    if errorlevel 1 (
        echo.
        echo   [提示] 直接安装失败，改用国内镜像重试...
        "%PY%" -m pip install --disable-pip-version-check -q -e . ^
            -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1
    )
    "%PY%" -c "import fengcode" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   [错误] 依赖安装失败。请手动执行：
        echo          "%PY%" -m pip install -e .
        echo.
        pause
        exit /b 1
    )
) else (
    echo         依赖已就绪
)

REM ---------- 3. 首次运行提示 ----------
"%PY%" -c "from fengcode.config import get_manager as g; m=g(); import sys; sys.exit(0 if any(m.resolve_api_key(p) for p in m.config.providers) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   --------------------------------------------------------
    echo     提示：还没有配置模型
    echo   --------------------------------------------------------
    echo     服务启动后会在浏览器打开界面，
    echo     请到「设置 - 模型供应商」里添加一个，
    echo     或点「从环境变量导入」把已有配置搬过来。
    echo   --------------------------------------------------------
    echo.
)

REM ---------- 4. 启动 ----------
echo   [3/4] 启动服务...
echo   [4/4] 浏览器会自动打开
echo.
echo   关闭此窗口即可停止服务。按 Ctrl+C 也能停止。
echo.

"%PY%" -m fengcode.cli.main serve --open

if errorlevel 1 (
    echo.
    echo   [错误] 服务异常退出。常见原因：
    echo         1. 端口被占用 —— 用「设置」改端口，或关掉占用的程序
    echo         2. 依赖缺失   —— 运行 "%PY%" -m pip install -e .
    echo         3. 配置损坏   —— 删除 %%APPDATA%%\..\ 下的 .fengcode/config/config.toml
    echo.
    echo   详细自检：  fengcode doctor
    echo.
    pause
)
endlocal
