$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $Root

python (Join-Path $Root 'tools\render_config.py') --check
if ($LASTEXITCODE -ne 0) { throw 'generated/ is stale - run tools/render_config.py' }

python (Join-Path $Root 'tools\verify_kit.py')
if ($LASTEXITCODE -ne 0) { throw 'verify_kit failed' }

python -m unittest discover -s (Join-Path $Root 'tests') -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw 'unit tests failed' }

'windows smoke tests passed'
Pop-Location
