@echo off
chcp 65001 >nul 2>&1
setlocal
title 构建 Fengcode exe

cd /d "%~dp0"

echo.
echo   ========================================================
echo     构建 Fengcode 可执行文件
echo   ========================================================
echo.

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
    echo   [错误] 没有找到 Python
    pause
    exit /b 1
)

echo   [1/3] 检查 PyInstaller...
"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo         安装 PyInstaller...
    "%PY%" -m pip install --disable-pip-version-check -q pyinstaller
    if errorlevel 1 (
        "%PY%" -m pip install --disable-pip-version-check -q pyinstaller ^
            -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
)
echo         就绪

echo   [2/3] 确保项目已安装...
"%PY%" -c "import fengcode" >nul 2>&1
if errorlevel 1 (
    "%PY%" -m pip install --disable-pip-version-check -q -e .
)

echo   [3/3] 打包中（约 1-3 分钟，请稍候）...
echo.
"%PY%" -m PyInstaller fengcode.spec --noconfirm --clean --distpath dist --workpath build

if errorlevel 1 (
    echo.
    echo   [错误] 打包失败。请查看上方输出。
    pause
    exit /b 1
)

echo.
echo   ========================================================
echo     构建完成
echo   ========================================================
echo.
echo     产物位置：dist\Fengcode\
echo     可执行文件：dist\Fengcode\Fengcode.exe
echo.
echo     双击 Fengcode.exe 即可启动（会自动打开浏览器）。
echo.
echo     分发方式：把整个 dist\Fengcode 文件夹压缩发给别人，
echo     对方解压后双击 Fengcode.exe 就能用，无需装 Python。
echo.
pause
endlocal
