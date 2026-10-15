@echo off
setlocal
cd /d "%~dp0"
if not exist "runtime\pythonw.exe" goto missing
if not exist "app\desktop_launcher.py" goto missing
start "Football Intelligence AI" "runtime\pythonw.exe" -I -X utf8 -B "app\desktop_launcher.py"
exit /b 0
:missing
echo Please extract the entire ZIP first, then double-click Start.cmd.
pause
exit /b 1
