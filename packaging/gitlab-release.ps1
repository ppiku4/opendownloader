$ErrorActionPreference = 'Stop'

function Run-Python {
    & $script:python @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $args" }
}

$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (-not (Test-Path $python)) { & "$PSScriptRoot/get_python313.ps1" }
if (-not (Test-Path $python)) { throw 'Python 3.13 installation failed' }
$version = & $python -c 'import opendownloader; print(opendownloader.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read application version' }
if ($env:CI_COMMIT_TAG -and $env:CI_COMMIT_TAG -ne "v$version") {
    throw "Tag $env:CI_COMMIT_TAG does not match version $version"
}
Run-Python -m pip install --disable-pip-version-check -r requirements.txt 'pyinstaller>=6.5' 'pytest>=8'
Run-Python -m pip install --disable-pip-version-check --only-binary=:all: 'libtorrent>=2.0.9'
Run-Python -m pytest -q --ignore=tests/e2e
Run-Python extension/build.py
Run-Python -m PyInstaller packaging/opendownloader.spec --noconfirm --clean
if (-not (Test-Path dist/OpenDownloader/OpenDownloader.exe)) { throw 'Missing built executable' }
Copy-Item -Recurse extension/dist/chromium dist/OpenDownloader/extension/chromium
Copy-Item -Recurse extension/dist/firefox dist/OpenDownloader/extension/firefox
Copy-Item packaging/README.txt dist/OpenDownloader/README.txt

$inno = Join-Path $env:TEMP 'opendownloader-innosetup.exe'
Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_5_4/innosetup-6.5.4.exe' -OutFile $inno
if ((Get-FileHash $inno -Algorithm SHA256).Hash -ne 'FA73BF47A4DA250D185D07561C2BFDA387E5E20DB77E4570004CF6A133CC10B1') {
    throw 'Inno Setup checksum mismatch'
}
$installer = Start-Process $inno -WindowStyle Hidden -Wait -PassThru -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/SP-'
if ($installer.ExitCode -ne 0) { throw 'Inno Setup installation failed' }
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" /Q "/DAppVersion=$version" packaging/installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }

New-Item -ItemType Directory -Force release | Out-Null
Copy-Item dist/OpenDownloader-Setup.exe release/
Compress-Archive -Path dist/OpenDownloader -DestinationPath "release/OpenDownloader-$version-portable.zip" -Force
Copy-Item extension/dist/opendownloader-chromium.zip release/
Copy-Item extension/dist/opendownloader-firefox.zip release/opendownloader-firefox.xpi
Get-ChildItem release -File | Sort-Object Name | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
} | Set-Content -Encoding ascii release/SHA256SUMS.txt
if ($env:CI_COMMIT_TAG) { Run-Python packaging/publish_gitlab.py }
