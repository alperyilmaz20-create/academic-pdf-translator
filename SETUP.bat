@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Academic PDF Translator - Setup

rem The installer tool (uv) is placed inside this folder; it also downloads Python.
set "UV_INSTALL_DIR=%~dp0_tools"
set "UV_NO_MODIFY_PATH=1"
set "UV=%~dp0_tools\uv.exe"

echo.
echo  Installing Academic PDF Translator. This can take a few minutes.
echo.

if exist "%UV%" goto uv_ready
echo  [1/3] Downloading the installer tool...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/0.11.16/install.ps1 | iex"
if not exist "%UV%" goto failed
:uv_ready

echo  [2/3] Preparing Python...
if exist ".venv\Scripts\python.exe" goto python_ready
"%UV%" venv --python 3.12 .venv
if errorlevel 1 goto failed
:python_ready

echo  [3/3] Installing the required packages...
"%UV%" pip install --python ".venv\Scripts\python.exe" -r REQUIREMENTS.TXT
if errorlevel 1 goto failed

echo.
echo  Setup complete.
if /i not "%~1"=="quiet" pause
exit /b 0

:failed
echo.
echo  ERROR: Setup could not be completed. Check your internet connection
echo  and run SETUP.bat again.
if /i not "%~1"=="quiet" pause
exit /b 1
