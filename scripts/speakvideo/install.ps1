param([switch]$Start)
$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Runner = Join-Path $PSScriptRoot 'run.ps1'
$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach($Pair in @(@('SpeakVideoQwen','qwen'), @('SpeakVideoTtsAgent','agent'))) {
    $Name = $Pair[0]
    $Mode = $Pair[1]
    $Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Existing -and ($Existing.Actions.Arguments -notlike "*$Runner*")) { throw "Task $Name already belongs to another launcher" }
    $Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`" -Mode $Mode" -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
    if ($Start) { Start-ScheduledTask -TaskName $Name }
    Write-Output "Registered $Name (startup + automatic failure restart)"
}
