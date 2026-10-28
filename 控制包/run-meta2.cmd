@echo off
chcp 65001 >nul
rem TF-META2 low-level control launcher (scan / single action test).
rem usage:
rem   run-meta2.cmd --scan
rem   run-meta2.cmd --vib 30 --sec 2
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.venv\Scripts\python.exe" (
  echo [x] .venv not found. Run install.ps1 first.
  exit /b 1
)
set "PYTHONIOENCODING=utf-8"
"%HERE%.venv\Scripts\python.exe" "%HERE%meta2.py" %*