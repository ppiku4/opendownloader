param([string]$Executable = 'dist/OpenDownloader/OpenDownloader.exe')
$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $Executable).Path
$report = Join-Path (Split-Path $exe) ('startup-check-' + [guid]::NewGuid().ToString('N') + '.json')
$process = Start-Process -FilePath $exe -ArgumentList '--smoke-test', ('"' + $report + '"') -WindowStyle Hidden -PassThru
if (-not $process.WaitForExit(60000)) {
    Stop-Process -Id $process.Id -Force
    throw 'Packaged startup check timed out'
}
if (-not (Test-Path -LiteralPath $report)) { throw "Packaged startup check produced no report (exit $($process.ExitCode))" }
$result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
Get-Content -LiteralPath $report
Remove-Item -LiteralPath $report
if ($process.ExitCode -ne 0 -or -not $result.ok) { throw 'Packaged application failed its startup check' }
