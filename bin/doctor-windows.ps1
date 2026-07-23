[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$KitRoot = Split-Path -Parent $PSScriptRoot
$DeviceEnv = if ($env:DELEGATE_DEVICE_ENV) { $env:DELEGATE_DEVICE_ENV } else { Join-Path $env:LOCALAPPDATA 'delekit\device.env' }
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$Failed = $false

foreach ($command in @('claude', 'git')) {
    $resolved = Get-Command $command -ErrorAction SilentlyContinue
    if ($resolved) { Write-Host "OK   $command`t$($resolved.Source)" } else { Write-Host "MISS $command"; $Failed = $true }
}

if (Get-Command claude -ErrorAction SilentlyContinue) {
    try {
        $versionText = (& claude --version 2>$null | Out-String).Trim()
        $match = [regex]::Match($versionText, '\d+\.\d+\.\d+')
        if (-not $match.Success) {
            Write-Warning "Could not parse Claude Code version: $versionText"
        } elseif ([version]$match.Value -lt [version]'2.1.211') {
            Write-Host "OLD  claude version`t$($match.Value) (need >= 2.1.211)"
            $Failed = $true
        } else {
            Write-Host "OK   claude version`t$($match.Value)"
        }
    } catch {
        Write-Warning 'Could not query Claude Code version.'
    }
}

$Python = @('py', 'python', 'python3') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if ($Python) {
    Write-Host "OK   python`t$Python"
    & $Python (Join-Path $KitRoot 'tools\verify_kit.py')
    if ($LASTEXITCODE -ne 0) { $Failed = $true }
} else {
    Write-Host 'MISS python'
    $Failed = $true
}

foreach ($path in @((Join-Path $ClaudeHome 'agents\delekit'), (Join-Path $ClaudeHome 'skills\orchestrate-delegates'))) {
    if (Test-Path -LiteralPath $path) { Write-Host "OK   wired`t$path" } else { Write-Host "MISS wired`t$path"; $Failed = $true }
}

function Import-SimpleEnvFile {
    param([Parameter(Mandatory)][string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) {
            Write-Host "BAD  device env line`t$raw"
            $script:Failed = $true
            continue
        }
        $key = $parts[0].Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            Write-Host "BAD  device env key`t$key"
            $script:Failed = $true
            continue
        }
        $value = $parts[1].Trim().Trim('"').Trim("'")
        $values[$key] = $value
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
    return $values
}

function Read-ModelsConfig {
    $values = @{}
    foreach ($raw in Get-Content -LiteralPath (Join-Path $KitRoot 'config\models.env')) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $values[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'") }
    }
    return $values
}

function Test-AutoCompactCap {
    # A persisted /autocompact value in settings.json caps every session,
    # including a [1m] parent: the model keeps its 1M window but compaction
    # fires at the cap. claudex only sanitizes environment variables, so the
    # settings file is the remaining silent cap.
    param([Parameter(Mandatory)][string]$SettingsPath, [Parameter(Mandatory)][string]$Label)
    if (-not (Test-Path -LiteralPath $SettingsPath)) { return }
    try {
        $cap = (Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json).autoCompactWindow
    } catch { return }
    if ($null -eq $cap) { return }
    if ($env:DELEGATE_PARENT_MODEL -like '*`[1m`]*' -and [int]$cap -lt 1000000) {
        Write-Host "BAD  autocompact cap`t$Label autoCompactWindow=$cap caps the [1m] parent; run /autocompact auto or remove the key from $SettingsPath"
        $script:Failed = $true
    } else {
        Write-Host "OK   autocompact cap`t$Label autoCompactWindow=$cap (deliberate?)"
    }
}

if (Test-Path -LiteralPath $DeviceEnv) {
    Write-Host "OK   device env`t$DeviceEnv"
    $null = Import-SimpleEnvFile -Path $DeviceEnv
    if ($env:DELEKIT_TANDY_CONTEXT_MODE -eq 'clientdata-272k') {
        Test-AutoCompactCap -SettingsPath (Join-Path $env:LOCALAPPDATA 'delekit\claude-profile\settings.json') -Label '272k profile'
    } else {
        Test-AutoCompactCap -SettingsPath (Join-Path $ClaudeHome 'settings.json') -Label 'user'
    }
    if ($env:DELEKIT_TANDY_CONTEXT_MODE -eq 'clientdata-272k') {
        $profile = Join-Path $env:LOCALAPPDATA 'delekit\claude-profile'
        if (-not $env:ANTHROPIC_AUTH_TOKEN) {
            Write-Host 'MISS 272k auth`tANTHROPIC_AUTH_TOKEN is required'
            $Failed = $true
        }
        foreach ($path in @((Join-Path $profile 'agents\delekit'), (Join-Path $profile 'skills\orchestrate-delegates'))) {
            if (Test-Path -LiteralPath $path) { Write-Host "OK   272k profile`t$path" }
            else { Write-Host "MISS 272k profile`t$path"; $Failed = $true }
        }
        $statePath = Join-Path $profile '.claude.json'
        try {
            $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ($state.clientDataCache.kelp_forest_sonnet -ne '272000' -or
                $state.clientDataCache.rowan_thicket.'claude-sonnet-4-6' -ne 272000 -or
                $state.autoCompactWindowsCache.'claude-sonnet-4-6' -ne 272000) {
                throw 'cache values are absent'
            }
            Write-Host "OK   272k cache`t$statePath"
        } catch {
            Write-Host 'MISS 272k cache`tlaunch claudex once to seed it'
            $Failed = $true
        }
    }
    if (-not $env:ANTHROPIC_BASE_URL) {
        Write-Host 'MISS gateway URL`tset ANTHROPIC_BASE_URL'
        $Failed = $true
    } elseif (-not $env:ANTHROPIC_AUTH_TOKEN -and -not $env:ANTHROPIC_API_KEY) {
        Write-Host 'MISS gateway auth`tset ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY'
        $Failed = $true
    } else {
        $headers = @{}
        if ($env:ANTHROPIC_AUTH_TOKEN) { $headers.Authorization = "Bearer $($env:ANTHROPIC_AUTH_TOKEN)" }
        elseif ($env:ANTHROPIC_API_KEY) { $headers.'x-api-key' = $env:ANTHROPIC_API_KEY }
        try {
            $uri = "$($env:ANTHROPIC_BASE_URL.TrimEnd('/'))/v1/models"
            $response = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec 5
            Write-Host "OK   proxy models`t$uri"
            $json = $response | ConvertTo-Json -Depth 20
            $models = Read-ModelsConfig
            foreach ($role in @('TERRA', 'LUNA', 'SOL')) {
                $alias = [string]$models["DELEGATE_ALIAS_$role"]
                if ($alias -and $json.Contains($alias)) { Write-Host "OK   $($role.ToLower()) alias`t$alias" }
                else {
                    # The live catalog drifts when config/models.env is rerendered but
                    # the fragment is never re-merged into the deployed config.yaml.
                    Write-Host "MISS $($role.ToLower()) alias`tnot served: $alias (re-merge generated\cliproxy\oauth-model-alias.yaml into the deployed config.yaml)"
                    $Failed = $true
                }
            }
        } catch {
            Write-Warning 'Proxy model endpoint is not reachable with the current local credential.'
        }
    }
} else {
    Write-Host "MISS device env`t$DeviceEnv"
    $Failed = $true
}

if ($Failed) { exit 1 }
exit 0
