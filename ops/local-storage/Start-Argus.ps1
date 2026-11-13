param([switch]$TestArchive)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$port = if ($TestArchive) { 8766 } else { 8765 }
$config = if ($TestArchive) { 'archive-api-test.json' } else { 'archive-api.json' }
$url = "http://127.0.0.1:$port/#/archive"
$python = Join-Path $root 'tools\python\python.exe'
$script = Join-Path $root 'app\web_api.py'
if (-not (Test-Path -LiteralPath (Join-Path $root ('private\' + $config)))) { throw 'Run setup-api.py first.' }
try { $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 2 } catch { $response = $null }
if (-not $response) {
    $arguments = '"' + $script + '" --root "' + $root + '" --config ' + $config + ' --port ' + $port
    Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "logs\api-$port.log") `
        -RedirectStandardError (Join-Path $root "logs\api-$port-error.log") | Out-Null
    $ready = $false
    for ($i=0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 300
        try { Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 1 | Out-Null; $ready=$true; break } catch {}
    }
    if (-not $ready) { throw 'Argus API did not start. See logs.' }
}
Start-Process $url
Write-Output 'Argus opened. Sign in with your individual login and key. Manage accounts with Manage-Users.cmd.'
