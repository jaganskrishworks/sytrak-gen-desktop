@echo off
rem Run SyTrak Gen from source (dev)
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe main.py
