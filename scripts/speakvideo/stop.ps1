param(
    [ValidateSet('all', 'agent', 'qwen')]
    [string]$Mode = 'all'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-Output 'Requesting administrator permission...'
    $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Mode', $Mode
    )
    exit $elevated.ExitCode
}

$Tasks = @{
    qwen = 'SpeakVideoQwen'
    agent = 'SpeakVideoTtsAgent'
}

function Get-ManagedProcesses([string]$ServiceMode) {
    $patterns = if ($ServiceMode -eq 'agent') {
        @('speakvideo_agent.py', 'run.ps1 -Mode agent')
    } else {
        @('-m qwen3_tts_web', 'run.ps1 -Mode qwen')
    }
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $line = [string]$_.CommandLine
        if ([string]::IsNullOrWhiteSpace($line)) { return $false }
        foreach ($pattern in $patterns) {
            if ($line -like "*$pattern*") { return $true }
        }
        return $false
    })
}

$Modes = if ($Mode -eq 'all') { @('agent', 'qwen') } else { @($Mode) }
foreach ($serviceMode in $Modes) {
    $taskName = $Tasks[$serviceMode]
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName
        Write-Output "Stopped $taskName."
    }
}

# Task Scheduler can return before the Python child exits and releases its lock.
$Deadline = (Get-Date).AddSeconds(30)
do {
    $remaining = @()
    foreach ($serviceMode in $Modes) {
        $remaining += Get-ManagedProcesses $serviceMode
    }
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $Deadline)

if ($remaining.Count -gt 0) {
    foreach ($process in $remaining) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Output 'Forced termination for processes that did not exit within 30 seconds.'
}

Write-Output "Stopped SpeakVideo mode: $Mode"
