@echo off
setlocal
cd /d "%~dp0"

set "HOST=%WEBMARS_USERS_HOST%"
if "%HOST%"=="" set "HOST=localhost"

set "PORT=%WEBMARS_USERS_PORT%"
if "%PORT%"=="" set "PORT=5000"

if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)

%PYTHON% -c "import flask, passlib" >nul 2>nul
if errorlevel 1 (
  echo Missing Python dependencies for the users service.
  echo Run one of the following from this folder:
  echo   python -m pip install -r requirements.txt
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

set "FLASK_APP=app"
set "FLASK_DEBUG=1"

echo WebMARS users service running at http://%HOST%:%PORT%/
echo Press Ctrl+C to stop.

%PYTHON% -m flask run --host %HOST% --port %PORT%
