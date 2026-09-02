@echo off
rem ============================================================
rem  Video Thumb Lister 2.0 - Windows GUI launcher
rem
rem  Double-click to start the GUI (opens maximized).
rem  Uses pythonw.exe so no console window flashes.
rem
rem  Usage:
rem    启动.bat                 -> launch GUI
rem ============================================================
cd /d "%~dp0"

set "PY="
if exist "C:\Users\excel\AppData\Local\Programs\Python\Python313\pythonw.exe" (
  set "PY=C:\Users\excel\AppData\Local\Programs\Python\Python313\pythonw.exe"
)
if not defined PY if exist "C:\Users\excel\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" (
  set "PY=C:\Users\excel\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
)
if not defined PY (
  where pythonw >nul 2>nul
  if not errorlevel 1 set "PY=pythonw"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found. Please install Python 3 and retry.
  pause
  exit /b 1
)

start "" "%PY%" "%~dp0main.py" %*
