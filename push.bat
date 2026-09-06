@echo off
setlocal
cd /d "%~dp0"

git config --get user.name >nul 2>&1
if errorlevel 1 git config user.name "hamburger445"
git config --get user.email >nul 2>&1
if errorlevel 1 git config user.email "hamburger445@users.noreply.github.com"

git add -A
git commit --allow-empty -m "Update"
if errorlevel 1 (
  echo Commit failed.
  pause
  exit /b 1
)

git push origin HEAD:main
if errorlevel 1 (
  echo Push failed.
  pause
  exit /b 1
)
echo Pushed.
pause
