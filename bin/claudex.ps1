# Deliberately no param()/[CmdletBinding()] block. PowerShell resolves partial
# parameter names against an advanced function's common parameters, so a declared
# parameter set makes `claudex -p "..."` bind to -PipelineVariable and die with
# "Cannot validate argument ... not a valid variable name" before claude starts.
# Claude Code's own -p/--print is the headless flag, so that collision is not
# hypothetical. Reading raw $args passes every flag through untouched.
$ClaudeArguments = $args

$ErrorActionPreference = 'Stop'
$KitRoot = Split-Path -Parent $PSScriptRoot
$DeviceEnv = if ($env:DELEGATE_DEVICE_ENV) {
    $env:DELEGATE_DEVICE_ENV
} else {
    Join-Path $env:LOCALAPPDATA 'delekit\device.env'
}

function Import-SimpleEnvFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) { throw "Invalid line in ${Path}: $raw" }
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "Invalid key in ${Path}: $key" }
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
}

Import-SimpleEnvFile -Path $DeviceEnv

if (-not $env:ANTHROPIC_BASE_URL) {
    throw "Set ANTHROPIC_BASE_URL in $DeviceEnv or the process environment."
}
if (-not $env:ANTHROPIC_AUTH_TOKEN -and -not $env:ANTHROPIC_API_KEY) {
    throw "Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in $DeviceEnv or the process environment."
}

Remove-Item Env:CLAUDE_CODE_SUBAGENT_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:CLAUDE_CODE_AUTO_COMPACT_WINDOW -ErrorAction SilentlyContinue
Remove-Item Env:CLAUDE_CODE_MAX_CONTEXT_TOKENS -ErrorAction SilentlyContinue
Remove-Item Env:DISABLE_COMPACT -ErrorAction SilentlyContinue
if (-not $env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY) {
    $env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = '1'
}
if (-not $env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT) {
    $env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT = '1'
}
if (-not $env:CLAUDE_CODE_ATTRIBUTION_HEADER) {
    $env:CLAUDE_CODE_ATTRIBUTION_HEADER = '0'
}
if (-not $env:ENABLE_TOOL_SEARCH) {
    $env:ENABLE_TOOL_SEARCH = 'false'
}
$env:DELEKIT_ROOT = $KitRoot

$ContextMode = if ($env:DELEKIT_TANDY_CONTEXT_MODE) { $env:DELEKIT_TANDY_CONTEXT_MODE } else { 'clientdata-272k' }
switch ($ContextMode) {
    { $_ -eq 'native-200k' -or $_ -eq '' } { break }
    'clientdata-272k' {
        if (-not $env:ANTHROPIC_AUTH_TOKEN) {
            throw 'clientdata-272k requires ANTHROPIC_AUTH_TOKEN.'
        }
        Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
        $env:CLAUDE_CONFIG_DIR = Join-Path $env:LOCALAPPDATA 'delekit\claude-profile'
        $Python = @('py', 'python', 'python3') |
            Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } |
            Select-Object -First 1
        if (-not $Python) { throw 'clientdata-272k requires Python 3.' }
        & $Python (Join-Path $KitRoot 'tools\seed_claude_context_cache.py')
        if ($LASTEXITCODE -ne 0) { throw 'Failed to seed the Claude Code context cache.' }
        if (-not (Test-Path -LiteralPath (Join-Path $env:CLAUDE_CONFIG_DIR 'agents\delekit'))) {
            throw 'The isolated 272k profile is not installed. Run bin\install-windows.ps1.'
        }
        break
    }
    default { throw "Unknown DELEKIT_TANDY_CONTEXT_MODE: $ContextMode" }
}

# Claude Code persists a /model choice into the *global* user settings file, so
# picking a gateway-only alias inside a gateway session leaks it to every later
# launch, including direct-to-Anthropic ones, which then fail with "model may
# not exist". Pinning the parent per launcher makes each one self-consistent
# regardless of what settings.json currently holds. An explicit --model on the
# command line still wins, so `claudex --model fable` keeps working.
$ParentArguments = @()
if ($env:DELEGATE_PARENT_MODEL) {
    $userSetModel = $false
    foreach ($arg in $ClaudeArguments) {
        if ($arg -eq '--model' -or $arg -eq '-m' -or $arg -like '--model=*') { $userSetModel = $true; break }
    }
    if (-not $userSetModel) { $ParentArguments = @('--model', $env:DELEGATE_PARENT_MODEL) }
}

& claude @ParentArguments @ClaudeArguments
exit $LASTEXITCODE
