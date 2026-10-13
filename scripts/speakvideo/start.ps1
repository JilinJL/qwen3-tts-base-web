param(
    [ValidateSet('all', 'agent', 'qwen')]
    [string]$Mode = 'all'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Installer = Join-Path $PSScriptRoot 'install.ps1'

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

# Register missing tasks without starting both services indiscriminately.
& $Installer

function Get-TaskOrNull([string]$Name) {
    return Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
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

function Test-QwenReady {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 3
        return ($health.ready -eq $true)
    } catch {
        return $false
    }
}

$Modes = if ($Mode -eq 'all') { @('qwen', 'agent') } else { @($Mode) }
foreach ($serviceMode in $Modes) {
    $taskName = $Tasks[$serviceMode]
    $task = Get-TaskOrNull $taskName
    if (-not $task) { throw "Scheduled task $taskName was not registered" }

    if ($serviceMode -eq 'qwen' -and (Test-QwenReady)) {
        Write-Output 'Qwen is already healthy; skipped duplicate start.'
        continue
    }

    if ($task.State -eq 'Running') {
        Write-Output "$taskName is already running."
        continue
    }

    $existing = Get-ManagedProcesses $serviceMode
    if ($existing.Count -gt 0) {
        Write-Output "$serviceMode process is already running outside Task Scheduler; skipped duplicate start."
        continue
    }

    Start-ScheduledTask -TaskName $taskName
    Write-Output "Started $taskName."
}

Start-Sleep -Milliseconds 500
foreach ($serviceMode in $Modes) {
    $taskName = $Tasks[$serviceMode]
    $state = (Get-TaskOrNull $taskName).State
    Write-Output "$taskName state: $state"
}
