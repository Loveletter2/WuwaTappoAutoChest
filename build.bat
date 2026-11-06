@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 打包成单文件 exe：dist\WuwaTappoAutoChest.exe
rem 需要先安装依赖：pip install -r requirements.txt

where pyinstaller >nul 2>&1
if errorlevel 1 (
  echo 未找到 pyinstaller，请先执行：
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)

pyinstaller --noconfirm --clean --onefile --windowed --uac-admin ^
  --name WuwaTappoAutoChest --icon icon.ico ^
  --add-data "ui;ui" --add-data "icon.ico;." --add-data "icon.png;." ^
  --hidden-import ui_server --hidden-import mini_ui ^
  --hidden-import pystray --hidden-import pystray._win32 ^
  autochest.py

if errorlevel 1 (
  echo.
  echo 打包失败，请检查上方日志。
  pause
  exit /b 1
)

echo.
echo 完成：dist\WuwaTappoAutoChest.exe
pause
