[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArguments
)

$ErrorActionPreference = 'Stop'
$deviceEnv = if ($env:DELEGATE_DEVICE_ENV) {
    $env:DELEGATE_DEVICE_ENV
} else {
    Join-Path $env:LOCALAPPDATA 'delekit\device.env'
}

# Save every value claudex.ps1 may import, plus the API key that must be cleared
# because it outranks the gateway token. This also keeps direct script calls
# safe even though the normal ccg.cmd entry point already runs in a child
# PowerShell process.
$keys = @('ANTHROPIC_API_KEY')
if (Test-Path -LiteralPath $deviceEnv) {
    foreach ($raw in Get-Content -LiteralPath $deviceEnv) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2 -and $parts[0].Trim() -match '^[A-Za-z_][A-Za-z0-9_]*$') {
            $keys += $parts[0].Trim()
        }
    }
}
$keys = @($keys | Select-Object -Unique)
$saved = @{}
foreach ($key in $keys) {
    $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', $null, 'Process')

try {
    & (Join-Path $PSScriptRoot 'claudex.ps1') @ClaudeArguments
    exit $LASTEXITCODE
} finally {
    foreach ($key in $keys) {
        [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
    }
}
