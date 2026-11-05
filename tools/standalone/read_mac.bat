@echo off
rem Double-click to read the device MAC. Extra arguments are passed on, e.g.:  read_mac.bat -Serial 69730362
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0read_mac.ps1" %*
pause
