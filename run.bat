@echo off
chcp 65001 >nul
title EC 海浪预报图
cd /d "%~dp0"

setlocal
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)
if not defined PY (
  echo [错误] 没有找到 Python，请先安装 Python 3.9+ 并勾选 Add to PATH。
  pause
  exit /b 1
)

"%PY%" ec_wave_chart.py %*
if errorlevel 1 pause
endlocal
