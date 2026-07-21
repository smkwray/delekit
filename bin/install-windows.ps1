[CmdletBinding()]
param(
    [switch]$Copy,
    [switch]$AddToUserPath,
    [string]$BinDir = (Join-Path $HOME 'bin')
)

$ErrorActionPreference = 'Stop'
$KitRoot = Split-Path -Parent $PSScriptRoot
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$DeviceDir = Join-Path $env:LOCALAPPDATA 'delekit'
$GatewayClaudeHome = Join-Path $DeviceDir 'claude-profile'

function Get-PythonCommand {
    foreach ($candidate in @('py', 'python', 'python3')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    throw 'Python 3 is required to render the templates.'
}

$Python = Get-PythonCommand
& $Python (Join-Path $KitRoot 'tools\render_config.py')
if ($LASTEXITCODE -ne 0) { throw 'Template rendering failed.' }
& $Python (Join-Path $KitRoot 'tools\verify_kit.py')
if ($LASTEXITCODE -ne 0) { throw 'Kit verification failed.' }

New-Item -ItemType Directory -Force -Path (Join-Path $ClaudeHome 'agents') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ClaudeHome 'skills') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $GatewayClaudeHome 'agents') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $GatewayClaudeHome 'skills') | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $DeviceDir | Out-Null

function Test-SamePath {
    param([Parameter(Mandatory)][string]$Left, [Parameter(Mandatory)][string]$Right)
    try {
        $a = [System.IO.Path]::GetFullPath($Left).TrimEnd('\')
        $b = [System.IO.Path]::GetFullPath($Right).TrimEnd('\')
        return [string]::Equals($a, $b, [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Install-DirectoryLink {
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Target)
    $Source = (Resolve-Path -LiteralPath $Source).Path

    if ($Copy) {
        $marker = Join-Path $Target '.delekit-copy-source'
        if (Test-Path -LiteralPath $Target) {
            $managed = $false
            if (Test-Path -LiteralPath $marker) {
                $recorded = (Get-Content -LiteralPath $marker -Raw).Trim()
                $managed = Test-SamePath -Left $recorded -Right $Source
            }
            if (-not $managed) { throw "Refusing to overwrite unmanaged path: $Target" }
            Remove-Item -LiteralPath $Target -Recurse -Force
            Copy-Item -LiteralPath $Source -Destination $Target -Recurse
            Set-Content -LiteralPath (Join-Path $Target '.delekit-copy-source') -Encoding UTF8 -Value $Source
            Write-Host "Refreshed copy: $Target"
            return
        }
        Copy-Item -LiteralPath $Source -Destination $Target -Recurse
        Set-Content -LiteralPath $marker -Encoding UTF8 -Value $Source
        Write-Host "Copied: $Target"
        return
    }

    if (Test-Path -LiteralPath $Target) {
        $item = Get-Item -LiteralPath $Target -Force
        foreach ($candidate in @($item.Target)) {
            if ($candidate -and (Test-SamePath -Left $candidate -Right $Source)) {
                Write-Host "Already linked: $Target -> $Source"
                return
            }
        }
        throw "Refusing to overwrite existing path: $Target"
    }
    try {
        New-Item -ItemType Junction -Path $Target -Target $Source | Out-Null
        Write-Host "Junction: $Target -> $Source"
    } catch {
        throw "Could not create junction $Target. Run with -Copy or use an approved filesystem location. $($_.Exception.Message)"
    }
}

function Install-CommandWrapper {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Target)
    $content = "@powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Target`" %*"
    if (Test-Path -LiteralPath $Path) {
        $existing = (Get-Content -LiteralPath $Path -Raw).TrimEnd("`r", "`n")
        if ($existing -eq $content) {
            Write-Host "Already installed: $Path"
            return
        }
        throw "Refusing to overwrite existing wrapper: $Path"
    }
    Set-Content -LiteralPath $Path -Encoding Ascii -Value $content
    Write-Host "Installed: $Path"
}

Install-DirectoryLink -Source (Join-Path $KitRoot 'generated\claude\agents') -Target (Join-Path $ClaudeHome 'agents\delekit')
Install-DirectoryLink -Source (Join-Path $KitRoot 'generated\claude\skills\orchestrate-delegates') -Target (Join-Path $ClaudeHome 'skills\orchestrate-delegates')
if (-not (Test-SamePath -Left $GatewayClaudeHome -Right $ClaudeHome)) {
    Install-DirectoryLink -Source (Join-Path $KitRoot 'generated\claude\agents') -Target (Join-Path $GatewayClaudeHome 'agents\delekit')
    Install-DirectoryLink -Source (Join-Path $KitRoot 'generated\claude\skills\orchestrate-delegates') -Target (Join-Path $GatewayClaudeHome 'skills\orchestrate-delegates')
}

$ClaudeXTarget = Join-Path $KitRoot 'bin\claudex.ps1'
$DairyTarget = Join-Path $KitRoot 'bin\dairy.ps1'
$ClaudeXWrapper = Join-Path $BinDir 'claudex.cmd'
Install-CommandWrapper -Path $ClaudeXWrapper -Target $ClaudeXTarget
Install-CommandWrapper -Path (Join-Path $BinDir 'dairy.cmd') -Target $DairyTarget

$DeviceEnv = Join-Path $DeviceDir 'device.env'
# Carry over a device.env from the pre-rename location so a switchover keeps the
# gateway token without re-entry. No-op on a fresh install (old path absent).
$LegacyEnv = Join-Path $env:LOCALAPPDATA 'delegate-kit\device.env'
if ((-not (Test-Path -LiteralPath $DeviceEnv)) -and (Test-Path -LiteralPath $LegacyEnv)) {
    Copy-Item -LiteralPath $LegacyEnv -Destination $DeviceEnv
    Write-Host "Migrated existing configuration: $LegacyEnv -> $DeviceEnv"
}
if (-not (Test-Path -LiteralPath $DeviceEnv)) {
    Copy-Item -LiteralPath (Join-Path $KitRoot 'config\device.env.example') -Destination $DeviceEnv
    Write-Host "Created local credential template: $DeviceEnv"
} else {
    Write-Host "Kept existing local configuration: $DeviceEnv"
}

if ($AddToUserPath) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($current -split ';' | Where-Object { $_ })
    if ($entries -notcontains $BinDir) {
        $newPath = if ($current) { "$current;$BinDir" } else { $BinDir }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Host 'Added command directory to the user PATH. Open a new terminal.'
    }
}

Write-Host @"

Installation complete.

Next:
1. Edit $DeviceEnv. Do not put real credentials in the synced kit.
2. Merge the appropriate generated\cliproxy YAML fragment into CLIProxyAPI.
3. Restart CLIProxyAPI.
4. Open a new terminal and run: claudex
5. Start a new Claude Code session if the agents directory did not exist earlier.

Optional project setting: merge config\claude-settings.fragment.json to base
native worktrees on the current committed HEAD.
"@
