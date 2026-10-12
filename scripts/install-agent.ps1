param(
    [string]$ServiceName = 'SpeakVideoTtsAgent',
    [string]$NssmPath = 'nssm',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Inference environment not found. Run scripts/bootstrap.ps1 setup --yes first.'
}
$Nssm = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $Nssm) {
    throw 'nssm was not found. Install NSSM and pass -NssmPath to nssm.exe.'
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root '.logs') | Out-Null
if ($Remove) {
    & $Nssm.Source stop $ServiceName 2>$null
    & $Nssm.Source remove $ServiceName confirm
    exit $LASTEXITCODE
}
& $Nssm.Source install $ServiceName $Python '-m' 'qwen3_tts_web.agent'
& $Nssm.Source set $ServiceName AppDirectory $Root
& $Nssm.Source set $ServiceName AppEnvironmentExtra "QWEN3_TTS_ROOT=$Root"
& $Nssm.Source set $ServiceName AppStdout (Join-Path $Root '.logs/tts-agent.stdout.log')
& $Nssm.Source set $ServiceName AppStderr (Join-Path $Root '.logs/tts-agent.stderr.log')
& $Nssm.Source set $ServiceName AppExit Default Restart
& $Nssm.Source set $ServiceName AppRestartDelay 5000
& $Nssm.Source set $ServiceName Start SERVICE_AUTO_START
& $Nssm.Source start $ServiceName
Write-Output "Installed and started $ServiceName."
