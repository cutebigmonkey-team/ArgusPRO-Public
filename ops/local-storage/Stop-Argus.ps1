param([string]$Root = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($Root)
$expected = Join-Path $root 'tools\python\python.exe'
$scripts = @((Join-Path $root 'app\archive_api.py'), (Join-Path $root 'app\web_api.py'))
foreach ($port in @(8765,8766)) {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener.OwningProcess)
        if ($process.ExecutablePath -eq $expected -and ($scripts | Where-Object { $process.CommandLine.Contains($_) })) {
            Stop-Process -Id $process.ProcessId
        }
    }
}
Write-Output 'Local Argus API stopped. PostgreSQL was left running.'
