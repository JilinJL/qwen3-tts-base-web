$ErrorActionPreference = 'Stop'
$AgentPath = Join-Path $PSScriptRoot 'speakvideo_agent.py'
Stop-ScheduledTask -TaskName SpeakVideoTtsAgent
# Task Scheduler's stop returns before the child releases its exclusive lock.
$Deadline = (Get-Date).AddSeconds(30)
while (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$AgentPath*" }) {
    if ((Get-Date) -gt $Deadline) { throw 'Previous agent is still stopping; restart deferred' }
    Start-Sleep -Milliseconds 500
}
Start-ScheduledTask -TaskName SpeakVideoTtsAgent
Write-Output 'Agent restarted after the previous process exited'
