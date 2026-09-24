# Устанавливает задачу автозапуска ДомПульса для текущего пользователя.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$startup = Join-Path $PSScriptRoot 'start-local-windows.ps1'
$account = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startup`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'DomPulseLocal' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Запуск бота и Mini App ДомПульс после входа в Windows' -Force | Out-Null
Write-Output 'Автозапуск ДомПульса настроен для текущего пользователя.'
