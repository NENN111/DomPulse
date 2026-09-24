# Запуск локальных компонентов ДомПульса после входа в Windows.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$local = Join-Path $repo '.local'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$cloudflared = Join-Path $local 'cloudflared.exe'
$tokenFile = Join-Path $local 'cloudflare-token.txt'
New-Item -ItemType Directory -Path $local -Force | Out-Null

$pythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*$python*" })
function Start-PythonModule($module, $outLog, $errLog) {
    if ($pythonProcesses | Where-Object { $_.CommandLine -match ('-m\s+' + [regex]::Escape($module) + '(?:\s|$)') }) { return }
    Start-Process -FilePath $python -ArgumentList '-m', $module -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $local $outLog) -RedirectStandardError (Join-Path $local $errLog) | Out-Null
}

Start-PythonModule 'app.local_api' 'miniapp-api.stdout.log' 'miniapp-api.stderr.log'
Start-PythonModule 'app.public_miniapp' 'public-miniapp.stdout.log' 'public-miniapp.stderr.log'
Start-PythonModule 'app.polling' 'polling-current.out.log' 'polling-current.err.log'

$existingTunnel = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" | Where-Object { $_.CommandLine -like "*$tokenFile*" }
if (-not $existingTunnel -and (Test-Path -LiteralPath $cloudflared) -and (Test-Path -LiteralPath $tokenFile)) {
    Start-Process -FilePath $cloudflared -ArgumentList 'tunnel', '--region', 'us', '--protocol', 'http2', 'run', '--token-file', $tokenFile -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $local 'cloudflared-us.stdout.log') -RedirectStandardError (Join-Path $local 'cloudflared-us.stderr.log') | Out-Null
}
