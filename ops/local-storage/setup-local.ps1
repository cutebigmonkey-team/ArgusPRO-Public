param([string]$Root = 'D:\argusDB')
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($Root)
if (Test-Path -LiteralPath (Join-Path $Root 'postgres\PG_VERSION')) {
    throw 'A PostgreSQL cluster already exists here. Setup does not overwrite it.'
}
foreach ($folder in @('downloads','tools','app','inbox','media','logs','private','receipts','staging')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $folder) | Out-Null
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe (Join-Path $Root 'private') /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect local credentials' }
function Download-Checked($Url, $Name, $Sha256) {
    $archive = Join-Path $Root ('downloads\' + $Name)
    if (-not (Test-Path -LiteralPath $archive)) {
        & curl.exe -fsSL --retry 2 --output $archive $Url
        if ($LASTEXITCODE -ne 0) { throw 'Runtime download failed' }
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower() -ne $Sha256) {
        throw ('Checksum mismatch: ' + $Name)
    }
    return $archive
}
$pythonZip = Download-Checked 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip' 'python-3.13.17-embed-amd64.zip' '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
$postgresZip = Download-Checked 'https://sbp.enterprisedb.com/getfile.jsp?fileid=1260616' 'postgresql-17.11.zip' '80379b2c04d51c30225532e0ae04509899141e9957ed096fe749d7fd9df8f82f'
Expand-Archive -LiteralPath $pythonZip -DestinationPath (Join-Path $Root 'tools\python') -Force
Expand-Archive -LiteralPath $postgresZip -DestinationPath (Join-Path $Root 'tools') -Force
Add-Content -LiteralPath (Join-Path $Root 'tools\python\python313._pth') -Value '../../app'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'storage.py'),(Join-Path $PSScriptRoot 'schema.sql') -Destination (Join-Path $Root 'app')
foreach ($launcher in @('Receive-From-GitHub.cmd', 'Import-Package.cmd', 'Stop-PostgreSQL.cmd')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $launcher) -Destination $Root
}
$python = Join-Path $Root 'tools\python\python.exe'
$bin = Join-Path $Root 'tools\pgsql\bin'
$adminPassword = & $python -c 'import secrets; print(secrets.token_hex(32))'
$importPassword = & $python -c 'import secrets; print(secrets.token_hex(32))'
if ($LASTEXITCODE -ne 0 -or -not $adminPassword -or -not $importPassword) { throw 'Cannot generate database credentials' }
$pwfile = Join-Path $Root 'private\init-password.tmp'
[IO.File]::WriteAllText($pwfile, $adminPassword + "`n", [Text.UTF8Encoding]::new($false))
$data = Join-Path $Root 'postgres'
try {
    & (Join-Path $bin 'initdb.exe') -D $data -U argus_admin --encoding=UTF8 --locale=C --auth=scram-sha-256 "--pwfile=$pwfile"
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL initialization failed' }
} finally {
    Remove-Item -LiteralPath $pwfile -Force -ErrorAction SilentlyContinue
}
Add-Content -LiteralPath (Join-Path $data 'postgresql.conf') -Value "`nlisten_addresses = '127.0.0.1'`nport = 55432`npassword_encryption = 'scram-sha-256'`n"
$adminConfig = @{PGHOST='127.0.0.1';PGPORT='55432';PGDATABASE='postgres';PGUSER='argus_admin';PGPASSWORD=$adminPassword}
$importConfig = @{PGHOST='127.0.0.1';PGPORT='55432';PGDATABASE='argus';PGUSER='argus_import';PGPASSWORD=$importPassword}
$adminConfig | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'private\postgres-admin.json') -Encoding utf8
$importConfig | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'private\postgres.json') -Encoding utf8
& $python -c 'import sys; import storage; storage.start_database(sys.argv[1])' $Root
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL startup failed' }
$env:PGHOST='127.0.0.1'; $env:PGPORT='55432'; $env:PGDATABASE='postgres'; $env:PGUSER='argus_admin'; $env:PGPASSWORD=$adminPassword
try {
    # Generated passwords contain hex characters only, never user-supplied SQL.
    "CREATE ROLE argus_import LOGIN PASSWORD '$importPassword';`nCREATE DATABASE argus OWNER argus_import;" |
        & (Join-Path $bin 'psql.exe') -X -v ON_ERROR_STOP=1 -w
    if ($LASTEXITCODE -ne 0) { throw 'Database/role creation failed' }
} finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
& $python -c 'import sys; from pathlib import Path; import storage; storage.psql(sys.argv[1], Path(sys.argv[2]).read_text())' $Root (Join-Path $Root 'app\schema.sql')
if ($LASTEXITCODE -ne 0) { throw 'Archive schema creation failed' }
Write-Output ('Ready: ' + $Root + ' (PostgreSQL is loopback-only on port 55432)')
