#!/usr/bin/env pwsh
# herd: detached, resumable delegate workers (codex/claude). Thin shim onto the
# cross-platform supervisor in tools/delegate_supervisor.py. Runtime state is
# device-local and never synced; see docs/detached-runner.md.
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$kitRoot = Split-Path -Parent $here
$py = if ($env:DELEKIT_PYTHON) { $env:DELEKIT_PYTHON } else { 'python3' }

$modelsFile = Join-Path $kitRoot 'config/models.env'
if (-not $env:DELEGATE_MODELS_FILE -and (Test-Path $modelsFile)) {
    $env:DELEGATE_MODELS_FILE = $modelsFile
}

& $py (Join-Path $kitRoot 'tools/delegate_supervisor.py') @args
exit $LASTEXITCODE
