@echo off
REM Pure-ASCII launcher -- see start.bat for why.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\status.ps1"
