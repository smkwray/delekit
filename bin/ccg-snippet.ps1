# Backward-compatible profile helper. New Windows installs expose the synced
# ccg.cmd directly on PATH, so no profile function is required.
$script:DelekitCcgLauncher = Join-Path $PSScriptRoot 'ccg.cmd'
function ccg { & $script:DelekitCcgLauncher @args }
