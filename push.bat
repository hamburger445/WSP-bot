@echo off
setlocal
cd /d "%~dp0"

git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Update"
) else (
  git commit --allow-empty -m "Trigger deploy"
)

git push origin HEAD
if errorlevel 1 (
  echo Push failed.
  exit /b 1
)
echo Pushed.
