@echo off
chcp 65001 >nul 2>&1
title FileSquirrel

echo ========================================
echo   FileSquirrel - 自动文件整理工具
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 检查并安装依赖
if not exist ".venv" (
    echo [安装] 创建虚拟环境...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo [安装] 安装依赖...
    pip install -e . -q
) else (
    call .venv\Scripts\activate.bat
)

REM 运行
if "%1"=="" (
    python -m src.main daemon --now
) else (
    python -m src.main %*
)

pause
