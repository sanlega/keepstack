# Unit tests + e2e (the e2e skips itself if docker, the image or terraform is missing).
Set-Location (Join-Path $PSScriptRoot "..")
python -m unittest discover -s tests -v
exit $LASTEXITCODE
