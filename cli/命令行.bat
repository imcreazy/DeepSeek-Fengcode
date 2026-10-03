@echo off
chcp 65001 >nul 2>&1
title Fengcode CLI

REM ============================================================
REM  Fengcode 命令行（Windows）
REM  双击进入交互式对话；也可直接在终端里用 fengcode 命令
REM ============================================================

cd /d "%~dp0"

set PY=
for %%P in (python.exe) do (
    if exist "%%~$PATH:P" set PY=python
)
if "%PY%"=="" (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%ProgramFiles%\Python312\python.exe"
    ) do (
        if exist %%D if "%PY%"=="" set PY=%%~D
    )
)
if "%PY%"=="" (
    echo [错误] 没有找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

"%PY%" -c "import fengcode" >nul 2>&1
if errorlevel 1 (
    echo 首次运行，正在安装依赖...
    "%PY%" -m pip install --disable-pip-version-check -q -e .
)

"%PY%" -m fengcode.cli.main chat

if errorlevel 1 pause
