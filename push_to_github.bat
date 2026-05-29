@echo off
cd /d "%~dp0"
echo ============================================================
echo  Upload to GitHub : jhonkim91/auto_trading
echo ============================================================
echo.

REM 0) check git
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] git is not installed. Install from https://git-scm.com then retry.
    pause
    exit /b 1
)

REM 1) init repo if needed
if not exist ".git" (
    git init
    git branch -M main
)

REM 2) set remote
git remote remove origin >nul 2>&1
git remote add origin https://github.com/jhonkim91/auto_trading.git

REM 3) stage files (.gitignore excludes .env / token.dat / *.db / logs)
git add .

REM 4) safety: force-unstage sensitive files just in case
git reset -q .env token.dat trades.db 2>nul

echo.
echo ----- Files to upload. If you see .env or token.dat, STOP (do NOT type Y) -----
git status --short
echo ------------------------------------------------------------------------------
echo.
set /p OK="Type Y then Enter if .env / token.dat are NOT in the list: "
if /i not "%OK%"=="Y" (
    echo Cancelled.
    pause
    exit /b 0
)

REM 5) commit and push
git commit -m "Stock analysis and auto-trading (KIS API + Telegram + GUI)"
echo.
echo Pushing... a GitHub login window may appear on first push.
git push -u origin main
if errorlevel 1 goto pushfail

echo.
echo Done. Open https://github.com/jhonkim91/auto_trading
pause
exit /b 0

:pushfail
echo.
echo [NOTE] Push rejected? The repo probably already has a README.
echo Run these two lines, then run this script again:
echo     git pull origin main --allow-unrelated-histories
echo     git push -u origin main
pause
exit /b 1
