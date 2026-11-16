@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto no_python
py -3.11 --version >nul 2>&1
if errorlevel 1 goto no_python
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -3.11 -m venv .venv
    if errorlevel 1 goto failed
)
echo Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Running the synthetic demo...
".venv\Scripts\python.exe" scripts\run_demo.py
if errorlevel 1 goto failed
echo Done. See the output location printed above.
pause
exit /b 0

:no_python
echo Python 3.11 with the Windows py launcher is required.
echo Install Python 3.11 from python.org, then run this file again.
pause
exit /b 1

:failed
echo A step failed. Please keep this window open and report the error above.
pause
exit /b 1
