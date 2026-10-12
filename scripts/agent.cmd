@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0agent.ps1" %*
exit /b %ERRORLEVEL%
