# Unit tests + e2e (the e2e suites skip themselves if docker or terraform is missing).
Set-Location (Join-Path $PSScriptRoot "..")
# Tests import keepstack from src/ without installing it.
$env:PYTHONPATH = (Join-Path $PWD "src") + [IO.Path]::PathSeparator + $env:PYTHONPATH
python -m unittest discover -s tests -v
exit $LASTEXITCODE
