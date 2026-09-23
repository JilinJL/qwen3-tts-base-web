@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" serve %*
exit /b %ERRORLEVEL%
