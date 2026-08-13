@echo off
REM Pure-ASCII launcher. cmd.exe parses .bat with the system ANSI codepage,
REM so any non-ASCII text here would be mangled -- the real logic (with
REM Chinese messages) lives in scripts\start.ps1, which is read as UTF-8.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start.ps1"
