@echo off
REM Pure-ASCII updater. cmd.exe parses .bat with the system ANSI codepage,
REM so any non-ASCII text here would be mangled -- the real logic (with
REM Chinese messages) lives in scripts\update.ps1, which is read as UTF-8.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\update.ps1"
