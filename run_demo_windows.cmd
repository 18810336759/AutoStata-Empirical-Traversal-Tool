@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
if exist ".venv" if not exist ".venv\Scripts\python.exe" goto invalid_venv
if exist ".venv\Scripts\python.exe" goto check_venv
where py >nul 2>&1
if errorlevel 1 goto no_python
set "PYTHON_VERSION="
for %%V in (3.11 3.12 3.13 3.10) do (
    if not defined PYTHON_VERSION (
        py -%%V --version >nul 2>&1
        if not errorlevel 1 set "PYTHON_VERSION=%%V"
    )
)
if not defined PYTHON_VERSION goto no_python
echo Creating Python %PYTHON_VERSION% environment...
py -%PYTHON_VERSION% -m venv .venv
if errorlevel 1 goto failed

:check_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))"
if errorlevel 1 goto invalid_venv
echo Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Running the synthetic demo...
".venv\Scripts\python.exe" scripts\run_demo.py
if errorlevel 1 goto failed
echo Done. See the output location printed above.
if not defined CI pause
exit /b 0

:no_python
echo Python 3.10, 3.11, 3.12 or 3.13 with the Windows py launcher is required.
echo Install Python 3.11 from python.org, then run this file again.
if not defined CI pause
exit /b 1

:invalid_venv
echo Existing .venv is incomplete or uses an unsupported Python version.
echo Keep it for inspection; rename .venv before trying again.
if not defined CI pause
exit /b 1

:failed
echo A step failed. Please keep this window open and report the error above.
if not defined CI pause
exit /b 1
