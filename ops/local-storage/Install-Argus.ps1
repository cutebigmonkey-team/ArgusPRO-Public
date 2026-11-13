param([string]$Root = 'D:\argusDB', [string]$ShellZip = (Join-Path $PSScriptRoot 'shell.zip'), [switch]$ConfigureTest)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($Root)
if (-not (Test-Path -LiteralPath $ShellZip) -or -not (Test-Path -LiteralPath ($ShellZip + '.sha256'))) {
    throw 'Verified shell.zip and shell.zip.sha256 are required beside the installer.'
}
$expected = (Get-Content -LiteralPath ($ShellZip + '.sha256') -Raw).Trim()
if ((Get-FileHash -LiteralPath $ShellZip -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw 'Shell checksum mismatch' }
if (-not (Test-Path -LiteralPath (Join-Path $Root 'postgres\PG_VERSION'))) {
    & (Join-Path $PSScriptRoot 'setup-local.ps1') -Root $Root
}
& (Join-Path $PSScriptRoot 'Stop-Argus.ps1') -Root $Root
$python = Join-Path $Root 'tools\python\python.exe'
& $python -X utf8 (Join-Path $PSScriptRoot 'install-app.py') --root $Root --shell $ShellZip
if ($LASTEXITCODE -ne 0) { throw 'App installation failed' }
& $python -X utf8 (Join-Path $Root 'app\setup-api.py') --root $Root
if ($LASTEXITCODE -ne 0) { throw 'Local accounts setup failed' }
if ($ConfigureTest) {
    & $python -X utf8 (Join-Path $Root 'app\setup-api.py') --root $Root --test
    if ($LASTEXITCODE -ne 0) { throw 'Test API setup failed' }
}
Write-Output ('Ready. Create your own login/key with ' + (Join-Path $Root 'Manage-Users.cmd') + ', then run Open-Argus.cmd.')
