@echo off
setlocal
pushd "%~dp0" || exit /b 1
powershell.exe -NoProfile -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$folder = (Get-Location).Path;" ^
    "$python = Join-Path $folder '.venv-local\Scripts\python.exe';" ^
    "if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $folder '.venv\Scripts\python.exe' };" ^
    "if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment missing. See README.md.' };" ^
    "Start-Process -FilePath $python -ArgumentList 'monster_ocr.py' -WorkingDirectory $folder -Verb RunAs -WindowStyle Normal"
set "DUGE_ASSISTANT_EXIT=%errorlevel%"
popd
if not "%DUGE_ASSISTANT_EXIT%"=="0" pause
exit /b %DUGE_ASSISTANT_EXIT%
