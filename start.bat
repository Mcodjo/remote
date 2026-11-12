@echo off
chcp 65001 >nul
py -3 "%~dp0lanshare_rtc.py" || python "%~dp0lanshare_rtc.py"
pause
