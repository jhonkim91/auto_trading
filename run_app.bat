@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  한국투자증권 자동매매 - 단일 GUI 실행
echo ============================================

if exist ".venv\Scripts\python.exe" (
    set PYEXE=.venv\Scripts\python.exe
) else (
    set PYEXE=python
)

REM 필수 패키지 확인 후 자동 설치
%PYEXE% -c "import requests, telegram, dotenv" 2>nul
if errorlevel 1 (
    echo 필수 패키지를 설치합니다...
    %PYEXE% -m pip install requests python-telegram-bot python-dotenv PyYAML
)

%PYEXE% autotrader.py
pause
