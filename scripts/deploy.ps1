param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs)
& (Join-Path $PSScriptRoot 'bootstrap.ps1') setup @CliArgs
exit $LASTEXITCODE
