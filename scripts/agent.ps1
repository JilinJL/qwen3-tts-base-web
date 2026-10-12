param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AgentArgs)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$env:QWEN3_TTS_ROOT = $Root
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
Set-Location -LiteralPath $Root
$Python = Join-Path $Root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Inference environment not found. Run scripts/bootstrap.ps1 setup --yes first.'
}
& $Python -m qwen3_tts_web.agent @AgentArgs
exit $LASTEXITCODE
