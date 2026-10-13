param([ValidateSet('qwen','agent')][string]$Mode = 'agent')
$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$env:QWEN3_TTS_ROOT = $Root
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
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
