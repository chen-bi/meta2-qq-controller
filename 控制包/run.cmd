@echo off
chcp 65001 >nul
rem TF-META2 controller launcher - uses the bundled .venv, no system python needed.
rem usage:
rem   run.cmd --list
rem   run.cmd --preset rush --minutes 10
rem   run.cmd --preset chaos --minutes 0
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.venv\Scripts\python.exe" (
  echo [x] .venv not found. Run install.ps1 first, or rebuild the virtualenv.
  exit /b 1
)
set "PYTHONIOENCODING=utf-8"
"%HERE%.venv\Scripts\python.exe" "%HERE%presets.py" %*
