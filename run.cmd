@echo off
cd /d %~dp0

:: 手动指向本地 venv，绕过 activate.bat 里的硬编码路径
set "VENV=%~dp0.venv"
set "VIRTUAL_ENV=%VENV%"
set "PATH=%VENV%\Scripts;%PATH%"
set "PYTHONHOME="
set "PYTHONPATH=%~dp0"

:: 离线模式
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"

echo ==========================================
echo   Qwen3-TTS Voice Clone API is starting,please wait
echo   http://localhost:8001/docs
echo ==========================================

"%VENV%\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8001