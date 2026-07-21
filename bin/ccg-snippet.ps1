# Paste into your PowerShell profile ($PROFILE).
# Defines ccg: the same Claude Code launch as your normal alias, but routed
# through the local CLIProxyAPI gateway so one session can mix model families
# (Claude parent + delegate* subagents on the GPT profiles).
#
# Leave your existing launcher untouched: it stays the direct-to-Anthropic
# fallback, and keeps claude.ai connectors working.

function ccg {
    $vars = @()
    $saved = @{}
    # A stray API key would outrank the gateway token; park it for this launch.
    foreach ($k in @('ANTHROPIC_API_KEY')) {
        $vars += $k
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, $null, 'Process')
    }
    $dev = Join-Path $env:LOCALAPPDATA 'delekit\device.env'
    if (Test-Path -LiteralPath $dev) {
        foreach ($line in Get-Content -LiteralPath $dev) {
            $t = $line.Trim()
            if (-not $t -or $t.StartsWith('#')) { continue }
            $i = $t.IndexOf('=')
            if ($i -lt 1) { continue }
            $k = $t.Substring(0, $i).Trim()
            $v = $t.Substring($i + 1).Trim()
            $vars += $k
            $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
    else {
        Write-Warning "delekit device.env not found; launching without the gateway."
    }
    try {
        if (Get-Command claudex -ErrorAction SilentlyContinue) {
            & claudex --dangerously-skip-permissions @args
        } else {
            Write-Warning 'claudex is not installed; launching without delekit profile setup.'
            & claude --dangerously-skip-permissions @args
        }
    }
    finally { foreach ($k in $vars) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') } }
}
