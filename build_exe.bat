@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  단일 실행파일(.exe) 빌드 - PyInstaller
echo  (윈도우에서 1~2분 소요, 처음 1회만)
echo ============================================

python -m pip install pyinstaller requests python-telegram-bot python-dotenv PyYAML

REM autotrader.py 를 단일 exe 로. config.yaml 동봉.
pyinstaller --noconfirm --onefile --windowed ^
  --name "자동매매" ^
  --add-data "config.yaml;." ^
  --collect-all telegram ^
  --hidden-import requests ^
  --hidden-import dotenv ^
  --hidden-import yaml ^
  autotrader.py

echo.
echo ============================================
echo  완료!  dist\자동매매.exe 생성됨
echo  사용법: dist\자동매매.exe 를 이 폴더로 옮긴 뒤(또는 .env/config.yaml 을 exe 옆에 두고) 실행
echo  ※ .env 파일(앱키·토큰)이 exe 와 같은 폴더에 있어야 합니다.
echo ============================================
pause
