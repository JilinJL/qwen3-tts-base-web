param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$env:QWEN3_TTS_ROOT = $Root
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
Set-Location -LiteralPath $Root
$Yes = $CliArgs -contains '--yes'
$Offline = $env:QWEN3_TTS_OFFLINE -in @('true', '1')
foreach ($Argument in $CliArgs) {
    if ($Argument -eq '--offline') { $Offline = $true }
    if ($Argument -eq '--no-offline') { $Offline = $false }
}
if (($CliArgs -contains '--help') -or ($CliArgs -contains '-h')) {
    Write-Output 'Usage: scripts/bootstrap.ps1 [tui|doctor|setup|download|serve] [options]'
    Write-Output 'Options: --yes --device auto|mps|cuda:N --model 1.7B|0.6B --source domestic|official'
    Write-Output '         --offline --host ADDRESS --port PORT --install-system --skip-download'
    Write-Output '         --model-key base|base-small|tokenizer|customvoice|voicedesign --json'
    exit 0
}
function Confirm-Install([string]$Message) {
    if ($Offline) { throw 'Offline: bootstrap management tools online first.' }
    if ($Yes) { return }
    if ([Console]::IsInputRedirected) { throw "$Message; use --yes for non-interactive installation." }
    if ((Read-Host "$Message [y/N]") -notmatch '^(y|yes)$') { throw 'Cancelled.' }
}
function Invoke-Uv {
    & $script:Uv @args
    if ($LASTEXITCODE -ne 0) { throw "uv failed (exit $LASTEXITCODE)." }
}
$Python = Join-Path $Root '.venv-tools/Scripts/python.exe'
if (Test-Path -LiteralPath $Python) {
    & $Python -c 'import sys, qwen3_tts_web, textual, uv; assert sys.version_info[:2] == (3,11)' 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $Python -m qwen3_tts_web @CliArgs
        exit $LASTEXITCODE
    }
}
Confirm-Install 'Install/repair local management tools and acquire Python 3.11 if missing?'
$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($UvCommand) { $script:Uv = $UvCommand.Source }
elseif (Test-Path "$HOME/.local/bin/uv.exe") { $script:Uv = "$HOME/.local/bin/uv.exe" }
else {
    Confirm-Install 'Install uv from astral.sh in your user account (no PATH/profile changes)?'
    $Installer = Join-Path ([IO.Path]::GetTempPath()) (([guid]::NewGuid().ToString()) + '.ps1')
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1' -OutFile $Installer
        $env:UV_NO_MODIFY_PATH = '1'
        & powershell -NoProfile -ExecutionPolicy Bypass -File $Installer
        if ($LASTEXITCODE -ne 0) { throw 'uv installation failed.' }
    } finally { Remove-Item -LiteralPath $Installer -ErrorAction SilentlyContinue }
    $script:Uv = "$HOME/.local/bin/uv.exe"
}
if (Test-Path (Join-Path $Root '.venv-tools')) {
    if (-not (Test-Path -LiteralPath $Python)) { throw 'Existing .venv-tools is broken; rename it as a backup first.' }
    & $Python -c 'import sys; assert sys.version_info[:2] == (3,11)'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv-tools is incompatible; rename it as a backup first.' }
} else { Invoke-Uv venv --python 3.11 (Join-Path $Root '.venv-tools') }
$Index = 'https://pypi.tuna.tsinghua.edu.cn/simple'
if ($env:QWEN3_TTS_SOURCE -eq 'official') { $Index = 'https://pypi.org/simple' }
if ($env:QWEN3_TTS_PIP_INDEX) { $Index = $env:QWEN3_TTS_PIP_INDEX }
for ($i = 0; $i -lt ($CliArgs.Count - 1); $i++) {
    if (($CliArgs[$i] -eq '--source') -and ($CliArgs[$i+1] -eq 'official')) { $Index = 'https://pypi.org/simple' }
    if ($CliArgs[$i] -eq '--pip-index') { $Index = $CliArgs[$i+1] }
}
try { Invoke-Uv --no-config pip install --python $Python --index-url $Index --editable $Root }
catch { Invoke-Uv --no-config pip install --python $Python --index-url https://pypi.org/simple --editable $Root }
& $Python -m qwen3_tts_web @CliArgs
exit $LASTEXITCODE
