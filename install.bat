@echo off
REM One-time setup when running from source on Windows.
cd /d "%~dp0"
echo Installing Python packages...
python -m pip install -r requirements.txt || goto :err
python -m pip install --only-binary=:all: "libtorrent>=2.0.9" >nul 2>&1 || echo Note: torrent support is not available for this Python version - use Python 3.13 to get it.
echo Building browser extension...
python extension\build.py || goto :err
echo Registering browser integration...
python -m opendownloader --register-host || goto :err
echo.
echo Done. Next steps:
echo   1. Chrome/Edge/Brave: open chrome://extensions, enable Developer mode, "Load unpacked" -^> extension\dist\chromium
echo   2. Firefox: about:debugging -^> This Firefox -^> Load Temporary Add-on -^> extension\dist\firefox\manifest.json
echo   3. Start the app with run.bat (or double-click OpenDownloader.pyw)
pause
exit /b 0
:err
echo Setup failed.
pause
exit /b 1
