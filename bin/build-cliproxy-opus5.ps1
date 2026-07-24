#Requires -Version 5.1
<#
.SYNOPSIS
Windows parity build for the CLIProxyAPI Opus 5 bridge. Mirrors bin/build-cliproxy-opus5.sh.

.DESCRIPTION
Builds and installs a CLIProxyAPI binary patched to serve Claude models that
Anthropic already serves but the upstream CLIProxyAPI catalog has not yet
published -- currently claude-opus-5. Without the patch, ccg dies with "502
unknown provider for model claude-opus-5" while ccc (direct) works, because the
proxy's Claude catalog (embedded models.json + the remote router-for-me feed)
lags new Anthropic models and the OAuth channel has no config-level model add.
See docs/known-issues.md.

The patch is a no-op once the upstream catalog adds the model -- at that point
delete this script, the .sh, and patches/, and install a stock release. To
bridge a further new model, add one ensureClonedModel(...) line to
ensureBridgeModels in patches/cliproxy-claude-opus-5.patch.

Windows differs from macOS in three ways this script handles:
  * the proxy lives in a VERSION-PINNED directory (%LOCALAPPDATA%\CLIProxyAPI\<tag>),
    so a tag bump means a new directory, not an in-place binary swap;
  * config.yaml sits beside the binary and carries the client key, so it must be
    carried into the new version directory (auth-dir is version-independent and
    is left alone);
  * there is no launchd -- a hidden .vbs in the Startup folder pins the full
    binary and config paths, so it is rewritten to match the new directory.

.PARAMETER Tag
CLIProxyAPI git tag to build. Default v7.2.98 (matches the macOS build).

.PARAMETER ProxyRoot
Root holding the version directories. Default %LOCALAPPDATA%\CLIProxyAPI.

.PARAMETER NoRestart
Stage and install the new version but leave the running proxy alone.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File bin\build-cliproxy-opus5.ps1
#>
[CmdletBinding()]
param(
    [string]$Tag = $(if ($env:CLIPROXY_TAG) { $env:CLIPROXY_TAG } else { 'v7.2.98' }),
    [string]$ProxyRoot = (Join-Path $env:LOCALAPPDATA 'CLIProxyAPI'),
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$KitRoot = Split-Path -Parent $PSScriptRoot
$Patch = Join-Path $KitRoot 'patches\cliproxy-claude-opus-5.patch'
$StartupVbs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\delekit-cli-proxy-api.vbs'

function Resolve-Go {
    $cmd = Get-Command go -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # winget installs to Program Files but does not refresh the current session PATH.
    $fallback = Join-Path $env:ProgramFiles 'Go\bin\go.exe'
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    throw 'Go toolchain required. Install with: winget install --id GoLang.Go'
}

$Go = Resolve-Go
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git required.' }
if (-not (Test-Path -LiteralPath $Patch)) { throw "patch not found: $Patch" }

$Target = Join-Path $ProxyRoot $Tag
# Newest existing version directory supplies config.yaml and static/ for the new one.
$Current = Get-ChildItem -Path $ProxyRoot -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^v\d' -and $_.Name -ne $Tag } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("cliproxy-opus5-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Work | Out-Null
try {
    Write-Host "Cloning CLIProxyAPI $Tag ..."
    # git writes progress and detached-HEAD advice to stderr; do NOT redirect it
    # (in PowerShell 5.1 that wraps each line in an ErrorRecord and trips $ErrorActionPreference).
    git clone -q --depth 1 --branch $Tag https://github.com/router-for-me/CLIProxyAPI.git (Join-Path $Work 'src')
    if ($LASTEXITCODE -ne 0) { throw "git clone failed ($LASTEXITCODE)" }

    $Src = Join-Path $Work 'src'
    Write-Host "Applying $(Split-Path -Leaf $Patch) ..."
    git -C $Src apply --check $Patch
    if ($LASTEXITCODE -ne 0) { throw "patch does not apply cleanly to $Tag -- rebase the patch before shipping" }
    git -C $Src apply $Patch
    if ($LASTEXITCODE -ne 0) { throw "git apply failed ($LASTEXITCODE)" }

    Write-Host 'Building (go build) ...'
    $env:CGO_ENABLED = '0'
    Push-Location $Src
    try {
        & $Go build -ldflags "-s -w -X main.Version=$Tag-opus5-bridge" -o cli-proxy-api.exe ./cmd/server
        if ($LASTEXITCODE -ne 0) { throw "go build failed ($LASTEXITCODE)" }
    } finally { Pop-Location }

    $Built = Join-Path $Src 'cli-proxy-api.exe'
    if (-not (Test-Path -LiteralPath $Built)) { throw 'build produced no binary' }

    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    if (Test-Path -LiteralPath (Join-Path $Target 'cli-proxy-api.exe')) {
        $bak = Join-Path $Target ("cli-proxy-api.exe.bak-" + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
        Copy-Item (Join-Path $Target 'cli-proxy-api.exe') $bak -Force
        Write-Host "Backed up existing binary -> $bak"
    }

    # Carry the per-device config and web assets forward. config.yaml holds the
    # client key and is never sourced from the synced kit.
    if ($Current) {
        foreach ($item in 'config.yaml', 'static', 'config.example.yaml', 'LICENSE', 'README.md') {
            $from = Join-Path $Current.FullName $item
            $to = Join-Path $Target $item
            if ((Test-Path -LiteralPath $from) -and -not (Test-Path -LiteralPath $to)) {
                Copy-Item -LiteralPath $from -Destination $to -Recurse -Force
            }
        }
        Write-Host "Carried config/static forward from $($Current.Name)"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'config.yaml'))) {
        Write-Warning "No config.yaml in $Target. Copy one in (with api-keys and auth-dir) before starting."
    }

    $running = @(Get-Process -Name 'cli-proxy-api' -ErrorAction SilentlyContinue)
    if ($running -and -not $NoRestart) {
        foreach ($p in $running) { Write-Host "Stopping PID $($p.Id)"; Stop-Process -Id $p.Id -Force }
        Start-Sleep -Seconds 2
    }

    Copy-Item -LiteralPath $Built -Destination (Join-Path $Target 'cli-proxy-api.exe') -Force
    Write-Host "Installed: $Target\cli-proxy-api.exe"

    # Repoint the Startup launcher; it pins absolute versioned paths.
    if (Test-Path -LiteralPath $StartupVbs) {
        $vbsText = Get-Content -LiteralPath $StartupVbs -Raw
        $newVbs = [regex]::Replace($vbsText, [regex]::Escape($ProxyRoot) + '\\v[0-9][^"\\]*', ($Target -replace '\\', '\'))
        if ($newVbs -ne $vbsText) {
            Copy-Item -LiteralPath $StartupVbs "$StartupVbs.bak" -Force
            Set-Content -LiteralPath $StartupVbs -Value $newVbs -Encoding Ascii -NoNewline
            Write-Host "Repointed startup launcher -> $Tag"
        }
    } else {
        Write-Warning "Startup launcher not found: $StartupVbs (start the proxy yourself)"
    }

    if ($running -and -not $NoRestart -and (Test-Path -LiteralPath $StartupVbs)) {
        Start-Process -FilePath 'wscript.exe' -ArgumentList "`"$StartupVbs`"" -WindowStyle Hidden
        Start-Sleep -Seconds 5
        $now = Get-Process -Name 'cli-proxy-api' -ErrorAction SilentlyContinue
        if ($now) { Write-Host "Proxy restarted (PID $($now.Id))" } else { Write-Warning 'Proxy did not come back up; start it manually.' }
    } elseif (-not $NoRestart) {
        Write-Host 'No proxy was running; start it from the Startup launcher when ready.'
    }

    Write-Host ''
    Write-Host 'Verify (reads the client key from the deployed config):'
    Write-Host ('  $cfg = Get-Content "{0}\config.yaml" -Raw' -f $Target)
    Write-Host '  $key = [regex]::Match($cfg, ''(?m)^api-keys:\s*\r?\n\s+-\s+"([^"]+)"'').Groups[1].Value'
    Write-Host '  (Invoke-RestMethod http://127.0.0.1:8317/v1/models -Headers @{Authorization="Bearer $key"}).data.id'
    Write-Host ''
    Write-Host 'Expect claude-opus-5 in the list. A 401 "OAuth access token has been revoked"'
    Write-Host 'on a live call is a CREDENTIAL problem, not a bridge problem -- re-run:'
    Write-Host ('  & "{0}\cli-proxy-api.exe" -claude-login -config "{0}\config.yaml"' -f $Target)
} finally {
    Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
}
