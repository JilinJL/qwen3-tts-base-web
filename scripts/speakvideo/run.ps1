param([ValidateSet('qwen','agent')][string]$Mode = 'agent')
$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$env:QWEN3_TTS_ROOT = $Root
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
if ($Mode -eq 'agent') {
    # websockets may inherit the Windows system proxy, which can stall WSS.
    $ConfigPath = Join-Path $Root 'config.toml'
    $GatewayHost = $null
    if (Test-Path -LiteralPath $ConfigPath) {
        $GatewayLine = Get-Content -LiteralPath $ConfigPath | Where-Object {
            $_ -match '^\s*gateway_url\s*='
        } | Select-Object -First 1
        if ($GatewayLine -and $GatewayLine -match 'wss?://([^/"\s]+)') {
            $GatewayHost = $Matches[1]
        }
    }
    if ($GatewayHost) {
        $NoProxyEntries = @($env:NO_PROXY, $env:no_proxy) |
            Where-Object { $_ } |
            ForEach-Object { $_ -split ',' } |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ } |
            Select-Object -Unique
        $NoProxyEntries = @($NoProxyEntries) + $GatewayHost
        $env:NO_PROXY = (($NoProxyEntries | Select-Object -Unique) -join ',')
        $env:no_proxy = $env:NO_PROXY
        $env:WS_NO_PROXY = $env:NO_PROXY
    }
}
# uv's embedded launcher can reference a junction inaccessible through SSH.
# Launch the real interpreter with CPython's virtual environment marker instead.
$env:__PYVENV_LAUNCHER__ = Join-Path $Root '.venv\Scripts\python.exe'
$HomeLine = Get-Content (Join-Path $Root '.venv\pyvenv.cfg') | Where-Object { $_ -match '^home = ' }
$InterpreterHome = $HomeLine -replace '^home = ', ''
$Python = Join-Path $InterpreterHome.Trim() 'python.exe'
if (!(Test-Path $Python)) { throw 'Real Python interpreter is unavailable' }
$LogDir = Join-Path $Root '.speakvideo\logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null
Set-Location -LiteralPath $Root
if ($Mode -eq 'qwen') { $Arguments = @('-m', 'qwen3_tts_web', 'serve', '--yes') }
else { $Arguments = @((Join-Path $PSScriptRoot 'speakvideo_agent.py')) }
$Process = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Root -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $LogDir "$Mode.stdout.log") -RedirectStandardError (Join-Path $LogDir "$Mode.stderr.log")
exit $Process.ExitCode
