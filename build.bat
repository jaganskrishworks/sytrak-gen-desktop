@echo off
rem Build a standalone dist\SyTrakGen.exe
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements-dev.txt || goto :error
python -m pytest -q tests || goto :error
if not exist assets\icons\app.ico python tools\make_icon.py || goto :error
pyinstaller --noconfirm --clean sytrak_gen.spec || goto :error

echo.
echo Build complete: %cd%\dist\SyTrakGen.exe
exit /b 0

:error
echo Build FAILED.
exit /b 1
