@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 从源码启动（需要 Python 3.10+ 与 requirements.txt 中的依赖）
rem 桌宠以管理员权限运行，这里同样提权启动；启动后仍需在窗口里点"启动监控"。

set "PYW="
for /f "delims=" %%i in ('py -3w -c "import sys;print(sys.executable)" 2^>nul') do set "PYW=%%i"

if not defined PYW (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PYW set "PYW=%%i"
  )
)

if not defined PYW (
  echo 未找到 pythonw，请先安装 Python 3.10+ 并勾选 "Add Python to PATH"
  echo 然后执行：pip install -r requirements.txt
  pause
  exit /b 1
)

powershell -NoProfile -Command "Start-Process -FilePath $env:PYW -ArgumentList '\"%~dp0autochest.py\"' -Verb RunAs"
