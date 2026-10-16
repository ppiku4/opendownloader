@echo off
REM ============================================================
REM  OpenDownloader - build a standalone Windows app (.exe)
REM  Double-click this file. Output:
REM    dist\OpenDownloader-Setup.exe        - the installer to give to people
REM    dist\OpenDownloader\OpenDownloader.exe - the app itself (portable folder)
REM  Full log: build_log.txt (in the project folder)
REM ============================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "LOG=%ROOT%\build_log.txt"
echo OpenDownloader build %DATE% %TIME% > "%LOG%"

echo [1/6] Looking for Python...
REM Torrent support (libtorrent) has ready-made packages for Python 3.10 - 3.13 only, so a 3.13
REM installed in its standard folder is preferred over any newer Python on this PC.
set "PY313=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
set "PY="
call :find_python
if defined PY %PY% -c "import sys; sys.exit(0 if sys.version_info < (3, 14) else 1)" >nul 2>&1
if defined PY if not errorlevel 1 goto :python_ok

echo.
echo  ------------------------------------------------------------------------
if defined PY (
    echo   Your Python is too new for torrent support ^(libtorrent has no package
    echo   for it yet^). The app would build, but WITHOUT torrent downloads.
) else (
    echo   Python was not found.
)
echo.
echo   Recommended: Python 3.13 ^(64-bit^), next to any Python you already have.
echo     Download : https://www.python.org/downloads/windows/
echo                -^> newest "Python 3.13.x" -^> "Windows installer (64-bit)"
echo     Install  : click "Customize installation", keep "py launcher" ticked,
echo                "Install Python 3.13 for all users" UNticked, and keep the
echo                default location:
echo                  %LOCALAPPDATA%\Programs\Python\Python313
echo                "Add python.exe to PATH" is not needed.
echo     Then run this build script again - it finds Python 3.13 by itself.
echo.
echo   Or let this script do it now: it downloads the official installer from
echo   python.org, checks its signature and installs it into the folder above
echo   for your user only ^(no admin rights needed^).
echo  ------------------------------------------------------------------------
choice /C YN /N /M "  Download and install Python 3.13 now? [Y/N] "
if errorlevel 2 goto :python_skip
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\packaging\get_python313.ps1"
if errorlevel 1 (
    echo     Python 3.13 could not be installed automatically - see the message above.
    echo     Install it by hand as described, then run this script again.
    goto :python_skip
)
set "PY="
call :find_python
:python_skip
if not defined PY (
    echo     Python 3.10+ is needed to build OpenDownloader. Install Python 3.13 as described above.
    goto :fail
)
:python_ok
%PY% --version
%PY% --version >> "%LOG%" 2>&1

echo [2/6] Installing build tools (PySide6, requests, PyInstaller)...
echo       This can take a few minutes the first time.
%PY% -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.5" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo     pip install failed.
    goto :fail
)
REM Torrent support is optional: libtorrent only has ready-made packages for some Python versions.
%PY% -m pip install --disable-pip-version-check --only-binary=:all: "libtorrent>=2.0.9" >> "%LOG%" 2>&1
%PY% -c "import libtorrent" >nul 2>&1
if errorlevel 1 (
    echo     NOTE: torrent support will NOT be included - libtorrent has no package for this
    echo           Python version yet. Install Python 3.13 into
    echo           %LOCALAPPDATA%\Programs\Python\Python313 and run this script again.
    echo           Everything else works.
) else (
    echo       Torrent support: included.
)

echo [3/6] Building the browser extension...
%PY% extension\build.py >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

echo [4/6] Building OpenDownloader.exe with PyInstaller (1-3 minutes)...
REM Close a running OpenDownloader first - its files would be locked and it would
REM keep answering instead of the new build.
%PY% -c "import sys; sys.path.insert(0, '.'); from opendownloader.__main__ import _stop_running; _stop_running(15)" >> "%LOG%" 2>&1
taskkill /F /IM OpenDownloader.exe >nul 2>&1
if exist "dist\OpenDownloader" rmdir /s /q "dist\OpenDownloader"
if exist "dist\OpenDownloader" (
    echo     Couldn't remove the old dist\OpenDownloader folder - is a file in it open?
    goto :fail
)
%PY% -m PyInstaller packaging\opendownloader.spec --noconfirm --clean >> "%LOG%" 2>&1
if errorlevel 1 (
    echo     PyInstaller failed.
    goto :fail
)
if not exist "dist\OpenDownloader\OpenDownloader.exe" (
    echo     PyInstaller finished but OpenDownloader.exe is missing.
    echo     Your antivirus may have quarantined it - check Windows Security ^> Protection history,
    echo     allow the file, and run this script again.
    goto :fail
)

echo [5/6] Copying the browser extension...
xcopy /E /I /Y /Q "extension\dist\chromium" "dist\OpenDownloader\extension\chromium" >> "%LOG%" 2>&1
xcopy /E /I /Y /Q "extension\dist\firefox" "dist\OpenDownloader\extension\firefox" >> "%LOG%" 2>&1
copy /Y "packaging\README.txt" "dist\OpenDownloader\README.txt" >nul
REM ffmpeg and yt-dlp are NOT bundled - the app offers to download them when first needed.

echo [6/6] Creating the installer (OpenDownloader-Setup.exe)...
call :find_iscc
if not defined ISCC (
    echo       Inno Setup not found - downloading it ^(free, about 8 MB, one time^)...
    call :find_python
REM Sets PY to the best Python found: 3.13 in its standard folder, then 3.13-3.10 via the
REM py launcher, then whatever "python" / "py -3" is (may be too new for torrent support).
if exist "%PY313%" set PY="%PY313%"& exit /b 0
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined PY py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
)
if not defined PY python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1 && set "PY=py -3"
exit /b 0

:install_inno
    call :find_iscc
)
if not defined ISCC (
    echo     Could not get Inno Setup. The app was built, but not the installer.
    echo     Install Inno Setup 6 from https://jrsoftware.org/isdl.php and run this script again.
    goto :done_no_setup
)
set "APPVER=1.0.0"
for /f "usebackq delims=" %%V in (`call %PY% -c "import opendownloader; print(opendownloader.__version__)"`) do set "APPVER=%%V"
if exist "dist\OpenDownloader-Setup.exe" del /f /q "dist\OpenDownloader-Setup.exe"
"%ISCC%" /Q "/DAppVersion=%APPVER%" packaging\installer.iss >> "%LOG%" 2>&1
if errorlevel 1 (
    echo     Inno Setup failed to create the installer.
    goto :fail
)
if not exist "dist\OpenDownloader-Setup.exe" (
    echo     The installer is missing - your antivirus may have quarantined it.
    goto :fail
)

echo.
echo ============================================================
echo  SUCCESS
echo ============================================================
echo  Installer : %ROOT%\dist\OpenDownloader-Setup.exe
echo  App folder: %ROOT%\dist\OpenDownloader\  (portable, same program)
echo.
echo  Run OpenDownloader-Setup.exe to install. It adds Start-menu/desktop shortcuts,
echo  registers the browser integration and can start with Windows.
echo  ffmpeg / yt-dlp are offered for download the first time a video needs them.
echo.
start "" explorer /select,"%ROOT%\dist\OpenDownloader-Setup.exe"
pause
exit /b 0

:done_no_setup
echo.
echo  App built: %ROOT%\dist\OpenDownloader\OpenDownloader.exe
start "" explorer "%ROOT%\dist\OpenDownloader"
pause
exit /b 0

REM ---------------------------------------------------------------- helpers
:find_iscc
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"
exit /b 0

:find_python
REM Sets PY to the best Python found: 3.13 in its standard folder, then 3.13-3.10 via the
REM py launcher, then whatever "python" / "py -3" is (may be too new for torrent support).
if exist "%PY313%" set PY="%PY313%"& exit /b 0
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined PY py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
)
if not defined PY python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1 && set "PY=py -3"
exit /b 0

:install_inno
REM Official Inno Setup 6.5.4 release, verified by SHA-256, installed for this user only (no admin).
set "INNO_URL=https://github.com/jrsoftware/issrc/releases/download/is-6_5_4/innosetup-6.5.4.exe"
set "INNO_SHA=FA73BF47A4DA250D185D07561C2BFDA387E5E20DB77E4570004CF6A133CC10B1"
set "INNO_EXE=%TEMP%\innosetup-6.5.4.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri '%INNO_URL%' -OutFile '%INNO_EXE%'; if ((Get-FileHash '%INNO_EXE%' -Algorithm SHA256).Hash -ne '%INNO_SHA%') { Remove-Item '%INNO_EXE%'; throw 'Checksum mismatch' }" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo       Download failed or checksum mismatch - see build_log.txt
    exit /b 1
)
"%INNO_EXE%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /SP- >> "%LOG%" 2>&1
del /f /q "%INNO_EXE%" >nul 2>&1
exit /b 0

:fail
echo.
echo ============================================================
echo  BUILD FAILED. Last lines of build_log.txt:
echo ============================================================
powershell -NoProfile -Command "Get-Content -Tail 25 '%LOG%'"
echo.
echo  Full log: %LOG%
pause
exit /b 1
