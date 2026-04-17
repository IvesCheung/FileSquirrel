@echo off
chcp 65001 >nul 2>&1
title FileSquirrel

echo ========================================
echo   FileSquirrel
echo ========================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [SETUP] Creating venv...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo [SETUP] Installing dependencies...
    pip install pyyaml requests Pillow -q
) else (
    call .venv\Scripts\activate.bat
)

if "%~1"=="" (
    python -m src.main daemon --now
) else (
    python -m src.main %*
)

pause
