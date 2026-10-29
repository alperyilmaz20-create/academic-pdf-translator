@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Academic PDF Translator
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem First run: install
if exist ".venv\Scripts\python.exe" goto installed
echo  First run: installing...
call "%~dp0SETUP.bat" quiet
if not exist ".venv\Scripts\python.exe" goto end
:installed

rem Ask for the API key once and save it to API_KEY.TXT
if exist "API_KEY.TXT" goto have_key
:ask_key
echo.
echo  A Claude API key is needed (see README.md for how to get one).
echo  Copy the key, paste it into this window with a right-click and press Enter.
echo.
set "APIKEY="
set /p "APIKEY=  API key: "
if not defined APIKEY goto ask_key
if /i "%APIKEY:~0,7%"=="sk-ant-" goto save_key
echo.
echo  That does not look like a Claude API key (it should start with sk-ant-). Please try again.
goto ask_key
:save_key
> "API_KEY.TXT" echo CLAUDE_API_KEY = %APIKEY%
echo  Key saved.
:have_key

if not exist "articles" mkdir "articles"
echo.
".venv\Scripts\python.exe" translate.py
echo.
if exist "translations" start "" "translations"

:end
echo.
echo  Press any key to close this window.
pause >nul
