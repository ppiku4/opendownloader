# Downloads the newest official Python 3.13 (64-bit) installer from python.org and installs it
# for the current user only (no admin prompt) into the standard folder:
#     %LOCALAPPDATA%\Programs\Python\Python313
# Your other Python versions are left untouched, and PATH is not changed.
# The installer's digital signature must be valid and from the Python Software Foundation.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'

$index = (Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/').Content
$versions = [regex]::Matches($index, 'href="(3\.13\.\d+)/"') |
    ForEach-Object { [version]$_.Groups[1].Value } | Sort-Object -Descending -Unique
$url = $null
foreach ($v in $versions) {
    $candidate = "https://www.python.org/ftp/python/$v/python-$v-amd64.exe"
    try { Invoke-WebRequest -UseBasicParsing -Uri $candidate -Method Head | Out-Null; $url = $candidate; break } catch { }
}
if (-not $url) { throw 'No Python 3.13 Windows installer found on python.org' }
Write-Host "      Downloading $url"
$file = Join-Path $env:TEMP ([IO.Path]::GetFileName($url))
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file

$sig = Get-AuthenticodeSignature $file
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    Remove-Item $file -Force
    throw "The downloaded installer is not signed by the Python Software Foundation ($($sig.Status))"
}

$target = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313'
Write-Host "      Installing into $target (this takes a minute)..."
$args = "/quiet InstallAllUsers=0 TargetDir=`"$target`" Include_launcher=1 InstallLauncherAllUsers=0 " +
        "PrependPath=0 Include_test=0 Include_doc=0 Shortcuts=0 AssociateFiles=0"
$p = Start-Process -FilePath $file -ArgumentList $args -WindowStyle Hidden -Wait -PassThru
Remove-Item $file -Force -ErrorAction SilentlyContinue
if ($p.ExitCode -ne 0) { throw "Python installer exited with code $($p.ExitCode)" }
if (-not (Test-Path (Join-Path $target 'python.exe'))) { throw "python.exe not found in $target after install" }
Write-Host '      Python 3.13 installed.'
