[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('workspace', 'write', 'readonly', 'read', 'full')]
    [string]$Delegate,

    [ValidateSet('codex', 'claude', 'muse', 'agy')]
    [string]$Backend,

    [string]$Profile,

    [string]$Model,
    [string]$Effort,
    [string]$PromptFile,
    [string]$Prompt,
    [switch]$PromptStdin,
    [string]$ProjectRoot,

    [Alias('Sandbox')]
    [ValidateSet('read-only', 'workspace-write', 'danger-full-access')]
    [string]$Access,

    [switch]$Worktree,
    [ValidateSet('fail', 'ignore')]
    [string]$DirtyPolicy = 'fail',
    [switch]$NoAutoCommit,
    [switch]$NoPreamble,
    [switch]$Fast,
    [switch]$Json,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$KitRoot = Split-Path -Parent $PSScriptRoot
$ModelsFile = if ($env:DELEGATE_MODELS_FILE) {
    $env:DELEGATE_MODELS_FILE
} elseif (Test-Path -LiteralPath (Join-Path $KitRoot 'config\models.env')) {
    Join-Path $KitRoot 'config\models.env'
} else {
    Join-Path $PSScriptRoot 'models.env'
}

function Import-SimpleEnvFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "Missing config: $Path" }
    $values = @{}
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) { throw "Invalid config line: $raw" }
        $key = $parts[0].Trim()
        if ($key -notmatch '^[A-Z][A-Z0-9_]*$') { throw "Invalid config key: $key" }
        $values[$key] = $parts[1].Trim().Trim('"').Trim("'")
    }
    return $values
}
$Config = Import-SimpleEnvFile -Path $ModelsFile

function Get-ConfigValue {
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Default
    )
    if ($Config.ContainsKey($Key)) {
        $value = [string]$Config[$Key]
        if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
    }
    return $Default
}

switch ($Delegate) {
    { $_ -in @('workspace', 'write') } {
        $Mode = 'workspace'
        if (-not $Access) { $Access = Get-ConfigValue -Key 'RUNNER_DEFAULT_WRITE_ACCESS' -Default 'workspace-write' }
    }
    { $_ -in @('readonly', 'read') } {
        $Mode = 'readonly'
        if (-not $Access) { $Access = Get-ConfigValue -Key 'RUNNER_DEFAULT_READ_ACCESS' -Default 'read-only' }
    }
    'full' {
        $Mode = 'full'
        if (-not $Access) { $Access = 'danger-full-access' }
    }
}
if (-not $Backend) {
    $Backend = if ($env:DELEGATE_BACKEND) { $env:DELEGATE_BACKEND } else { Get-ConfigValue -Key 'RUNNER_DEFAULT_BACKEND' -Default 'codex' }
}
if ($Backend -notin @('codex', 'claude', 'muse', 'agy')) { throw "Unsupported backend from config/environment: $Backend" }
if ($Access -notin @('read-only', 'workspace-write', 'danger-full-access')) { throw "Unsupported access mode from config/environment: $Access" }

$ProfileExplicit = $PSBoundParameters.ContainsKey('Profile')
if (-not $Profile -and $env:DELEGATE_PROFILE) {
    $Profile = $env:DELEGATE_PROFILE
    $ProfileExplicit = $true
}
if ($Backend -eq 'codex') {
    if (-not $Profile) { $Profile = 'terra' }
    if ($Profile -notin @('terra', 'luna', 'sol')) { throw 'Codex profile must be terra, luna, or sol' }
} elseif ($Backend -eq 'muse') {
    if (-not $Profile) { $Profile = 'spark' }
    if ($Profile -notin @('spark')) { throw 'muse profile must be spark' }
} elseif ($Backend -eq 'agy') {
    if (-not $Profile) { $Profile = 'flash-high' }
    $legacyAgyProfiles = @{
        terra = 'flash-high'
        luna = 'flash-low'
        sol = 'pro-high'
    }
    if ($legacyAgyProfiles.ContainsKey($Profile)) {
        $replacement = $legacyAgyProfiles[$Profile]
        Write-Warning "Deprecated agy profile $Profile; use $replacement."
        $Profile = $replacement
    }
    if ($Profile -notin @('flash-high', 'flash-low', 'pro-high')) {
        throw 'agy profile must be flash-high, flash-low, or pro-high'
    }
} elseif ($ProfileExplicit) {
    throw "-Profile resolves a model only for the codex and agy backends; config/models.env holds their IDs. For -Backend $Backend, pass -Model explicitly."
} else {
    $Profile = ''
}

# agy has no Codex-style filesystem sandbox: headless agy is either plan
# (read-only, tool writes soft-denied) or --dangerously-skip-permissions
# (unrestricted, NOT workspace-confined). It cannot honor a confined
# workspace-write, so refuse it rather than granting host-wide writes.
if ($Backend -eq 'agy' -and $Access -notin @('read-only', 'danger-full-access')) {
    throw "agy has no confined workspace-write mode: headless agy is either plan (read-only) or --dangerously-skip-permissions (unrestricted, not workspace-confined). Run agy with readonly, or full for explicit unrestricted writes; use codex/claude for confined writes."
}

$sourceCount = @($PromptFile, $Prompt) | Where-Object { $_ } | Measure-Object | Select-Object -ExpandProperty Count
if ($PromptStdin) { $sourceCount++ }
if ($sourceCount -eq 0 -and [Console]::IsInputRedirected) { $PromptStdin = $true; $sourceCount = 1 }
if ($sourceCount -ne 1) { throw 'Choose exactly one of -PromptFile, -Prompt, or -PromptStdin.' }
if ($PromptFile) {
    if (-not (Test-Path -LiteralPath $PromptFile)) { throw "Prompt file not found: $PromptFile" }
    $TaskPrompt = Get-Content -LiteralPath $PromptFile -Raw
} elseif ($PromptStdin) {
    $TaskPrompt = [Console]::In.ReadToEnd()
} else {
    $TaskPrompt = $Prompt
}
if ([string]::IsNullOrWhiteSpace($TaskPrompt)) { throw 'Task prompt is empty.' }

$profileUpper = $Profile.Replace('-', '_').ToUpperInvariant()
if ($Backend -eq 'codex') {
    if (-not $Model) { $Model = $Config["DELEGATE_MODEL_$profileUpper"] }
    if (-not $Model) { throw "No model configured for profile $Profile" }
    if (-not $Effort) { $Effort = Get-ConfigValue -Key "DELEGATE_EFFORT_$profileUpper" -Default 'high' }
} elseif ($Backend -eq 'muse') {
    if (-not $Model) { $Model = $Config["DELEGATE_MUSE_MODEL_$profileUpper"] }
    if (-not $Model) { throw "No muse model configured for profile $Profile" }
    if (-not $Effort) { $Effort = Get-ConfigValue -Key "DELEGATE_MUSE_EFFORT_$profileUpper" -Default 'high' }
} elseif ($Backend -eq 'agy') {
    # agy slugs bake the effort tier into the name, so a profile resolves to a full
    # agy slug and no separate -Effort is sent. -Model overrides for a single run.
    if (-not $Model) { $Model = $Config["DELEGATE_AGY_MODEL_$profileUpper"] }
    if (-not $Model) { throw "No agy model configured for profile $Profile" }
}

function Find-ProjectRoot([string]$Start) {
    try {
        $gitRootOutput = & git -C $Start rev-parse --show-toplevel 2>$null
        if ($LASTEXITCODE -eq 0 -and $gitRootOutput) { return (($gitRootOutput -join "`n").Trim()) }
    } catch {}
    $current = (Resolve-Path -LiteralPath $Start).Path
    $markers = @('pyproject.toml', 'package.json', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle', 'settings.gradle', '.git')
    while ($current) {
        foreach ($marker in $markers) {
            if (Test-Path -LiteralPath (Join-Path $current $marker)) { return $current }
        }
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
    return (Resolve-Path -LiteralPath $Start).Path
}
if (-not $ProjectRoot) { $ProjectRoot = Find-ProjectRoot -Start (Get-Location).Path }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

if ($Worktree -and $Access -eq 'read-only') {
    Write-Warning 'Worktree isolation is unnecessary for read-only mode; disabling it.'
    $Worktree = $false
}

$StateRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'delekit' } else { Join-Path $HOME '.delekit' }
$LogDir = if ($env:DELEGATE_LOG_DIR) { $env:DELEGATE_LOG_DIR } else { Join-Path $StateRoot 'logs' }
$timestamp = "{0}_{1}" -f ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')), $PID
$prefix = Join-Path $LogDir "${timestamp}_${Mode}_${Backend}"
$PromptLog = "$prefix.prompt.md"
$StdoutLog = "$prefix.stdout.log"
$StderrLog = "$prefix.stderr.log"
$ReportFile = "$prefix.report.md"
$StatusFile = "$prefix.status.json"
$DoneFile = "$prefix.done"

$ExecutionRoot = $ProjectRoot
$Branch = ''
$WorktreeDir = ''
$BaseSha = ''
if ($Worktree) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git is required for -Worktree.' }
    & git -C $ProjectRoot rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) { throw '-Worktree requires a Git repository.' }
    $dirty = ((& git -C $ProjectRoot status --porcelain) -join "`n")
    if ($DirtyPolicy -eq 'fail' -and $dirty) {
        throw 'Main checkout is dirty. Commit/stash changes or use -DirtyPolicy ignore.'
    }
    if ($DirtyPolicy -eq 'ignore' -and $dirty) {
        Write-Warning 'The worktree starts from committed HEAD; uncommitted parent changes are not included.'
    }
    $BaseSha = ((& git -C $ProjectRoot rev-parse HEAD) -join '').Trim()
    $Branch = "delegate/${Mode}-${timestamp}"
    $WorktreeDir = Join-Path (Join-Path $ProjectRoot '.worktrees') "${Mode}-${timestamp}"
    $ExecutionRoot = $WorktreeDir
}

$parts = New-Object 'System.Collections.Generic.List[string]'
if (-not $NoPreamble) {
    switch ($Access) {
        'read-only' { $parts.Add('**Access: read-only.** Do not create, edit, or delete files. Return the complete result in the final message.') }
        'workspace-write' { $parts.Add('**Access: workspace-write.** Work only inside the current project or worktree. Return outcome, changed files, validation, and blockers in the final message.') }
        'danger-full-access' { $parts.Add('**Access: unrestricted and explicitly authorized for this run.** Minimize changes outside the project and report every external effect.') }
    }
    if ($Worktree) { $parts.Add('**Isolation: Git worktree.** Stay in the current worktree; do not switch branches, touch the main checkout, push, merge, or remove the worktree.') }
}
$parts.Add($TaskPrompt)
$ComposedPrompt = $parts -join "`n`n"

if ($DryRun) {
    $dry = [ordered]@{
        dry_run = $true
        backend = $Backend
        profile = $Profile
        model = $Model
        effort = $Effort
        access = $Access
        project_root = $ProjectRoot
        execution_root = $ExecutionRoot
        worktree = [bool]$Worktree
        report = $ReportFile
        prompt = $ComposedPrompt
    }
    if ($Json) {
        $dry | ConvertTo-Json -Depth 5
    } else {
        Write-Output "backend=$Backend"
        Write-Output "profile=$Profile"
        Write-Output "model=$Model"
        Write-Output "effort=$Effort"
        Write-Output "access=$Access"
        Write-Output "project_root=$ProjectRoot"
        Write-Output "execution_root=$ExecutionRoot"
        Write-Output "worktree=$([bool]$Worktree)"
        Write-Output "report=$ReportFile"
        Write-Output '--- composed prompt ---'
        Write-Output $ComposedPrompt
    }
    exit 0
}

if (-not (Get-Command $Backend -ErrorAction SilentlyContinue)) { throw "$Backend CLI not found in PATH." }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$ttlText = Get-ConfigValue -Key 'RUNNER_LOG_TTL_DAYS' -Default '7'
$ttl = 0
if (-not [int]::TryParse($ttlText, [ref]$ttl) -or $ttl -lt 0) { throw 'RUNNER_LOG_TTL_DAYS must be a nonnegative integer.' }
Get-ChildItem -LiteralPath $LogDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '\.(stdout|stderr)\.log$' -and $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddDays(-$ttl) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

if ($Worktree) {
    $Python = @('py', 'python', 'python3') |
        Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } |
        Select-Object -First 1
    if (-not $Python) { throw 'Python 3 is required to create a worktree.' }
    $created = & $Python (Join-Path $KitRoot 'tools\worktree_manager.py') create `
        --project-root $ProjectRoot --name "${Mode}-${timestamp}" `
        --branch $Branch --base HEAD
    if ($LASTEXITCODE -ne 0) { throw "Failed to create worktree: $WorktreeDir" }
    $WorktreeDir = (@($created)[-1]).Trim()
    $ExecutionRoot = $WorktreeDir
}
Set-Content -LiteralPath $PromptLog -Value $ComposedPrompt -Encoding UTF8

$oldEnv = @{}
foreach ($name in @('CI', 'GIT_TERMINAL_PROMPT', 'GIT_PAGER', 'PAGER', 'NO_COLOR')) {
    $oldEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$env:CI = '1'; $env:GIT_TERMINAL_PROMPT = '0'; $env:GIT_PAGER = 'cat'; $env:PAGER = 'cat'; $env:NO_COLOR = '1'
$ExitCode = 0
try {
    switch ($Backend) {
        'codex' {
            $arguments = @('exec', '--cd', $ExecutionRoot, '--model', $Model, '-c', "model_reasoning_effort=$Effort")
            if ($Fast) { $arguments += @('--enable', 'fast_mode') }
            else { $arguments += @('--disable', 'fast_mode') }
            $arguments += @('--skip-git-repo-check', '--color', 'never', '-o', $ReportFile)
            if ($Access -eq 'danger-full-access') { $arguments += '--dangerously-bypass-approvals-and-sandbox' }
            else { $arguments += @('--sandbox', $Access, '-c', 'approval_policy=never') }
            $ComposedPrompt | & codex @arguments - 1> $StdoutLog 2> $StderrLog
            $ExitCode = $LASTEXITCODE
        }
        'claude' {
            $permissionMode = switch ($Access) { 'read-only' { 'plan' }; 'workspace-write' { 'acceptEdits' }; default { 'bypassPermissions' } }
            $arguments = @('-p', '-', '--output-format', 'text', '--no-session-persistence', '--permission-mode', $permissionMode, '--add-dir', $ExecutionRoot)
            if ($Model) { $arguments += @('--model', $Model) }
            if ($Effort) { $arguments += @('--effort', $Effort) }
            Push-Location $ExecutionRoot
            try { $ComposedPrompt | & claude @arguments 1> $StdoutLog 2> $StderrLog; $ExitCode = $LASTEXITCODE }
            finally { Pop-Location }
            if (Test-Path -LiteralPath $StdoutLog) { Copy-Item -LiteralPath $StdoutLog -Destination $ReportFile -Force }
        }
        'muse' {
            # Muse CLI, headless `exec`: the final answer is plain text on stdout
            # and the prompt is read from the composed prompt file written above.
            #
            # Approval and the sandbox are ON by default; approvals must be off or
            # a headless run blocks on them. Muse's default sandbox confines shell
            # writes to the workspace plus temp dirs (same shape as codex
            # workspace-write). --disable-write blocks only the non-shell write
            # tools, so read-only drops the shell too rather than overstating the
            # label. See docs/dairy-runner.md.
            $arguments = @('exec', '--model', $Model, '--reasoning-effort', $Effort, '--workspace', $ExecutionRoot)
            switch ($Access) {
                'read-only' { $arguments += @('--disable-approval', '--disable-write', '--disable-shell') }
                'workspace-write' { $arguments += '--disable-approval' }
                'danger-full-access' { $arguments += '--yolo' }
            }
            $arguments += @('--prompt-file', $PromptLog)
            # Resolve the application explicitly: an interactive profile may define
            # a `muse` function, and dairy's access boundary must not depend on
            # caller command precedence.
            $museExecutable = (Get-Command muse.exe -CommandType Application -ErrorAction Stop).Source
            Push-Location $ExecutionRoot
            try { & $museExecutable @arguments 1> $StdoutLog 2> $StderrLog; $ExitCode = $LASTEXITCODE }
            finally { Pop-Location }
            if (Test-Path -LiteralPath $StdoutLog) { Copy-Item -LiteralPath $StdoutLog -Destination $ReportFile -Force }
        }
        'agy' {
            # Antigravity CLI, headless print mode (-p): the prompt is the flag's
            # value and the final answer is plain text on stdout. Access is limited
            # to the two modes agy enforces headlessly (workspace-write is rejected
            # earlier): read-only -> plan (writes soft-denied); full ->
            # --dangerously-skip-permissions (unrestricted).
            $arguments = @()
            switch ($Access) {
                'read-only' { $arguments += @('--mode', 'plan') }
                'danger-full-access' { $arguments += '--dangerously-skip-permissions' }
            }
            $arguments += @('--add-dir', $ExecutionRoot)
            if ($Model) { $arguments += @('--model', $Model) }
            if ($Effort) { $arguments += @('--effort', $Effort) }
            $arguments += @('-p', $ComposedPrompt)
            # Resolve the application explicitly. An interactive PowerShell profile
            # may define an `agy` function with extra flags; dairy's access boundary
            # must not change according to caller command precedence.
            $agyExecutable = (Get-Command agy.exe -CommandType Application -ErrorAction Stop).Source
            Push-Location $ExecutionRoot
            try { & $agyExecutable @arguments 1> $StdoutLog 2> $StderrLog; $ExitCode = $LASTEXITCODE }
            finally { Pop-Location }
            if (Test-Path -LiteralPath $StdoutLog) { Copy-Item -LiteralPath $StdoutLog -Destination $ReportFile -Force }
        }
    }
} finally {
    foreach ($name in $oldEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $oldEnv[$name], 'Process') }
}

if (-not (Test-Path -LiteralPath $ReportFile) -and (Test-Path -LiteralPath $StdoutLog)) {
    Copy-Item -LiteralPath $StdoutLog -Destination $ReportFile
}

$invalidReport = -not (Test-Path -LiteralPath $ReportFile)
if (-not $invalidReport) {
    $reportText = (Get-Content -LiteralPath $ReportFile -Raw -ErrorAction SilentlyContinue)
    $normalizedReport = (($reportText -replace "`r", '') -split "`n" | Where-Object { $_.Trim() }) -join "`n"
    $invalidReport = [string]::IsNullOrWhiteSpace($normalizedReport) -or $normalizedReport -eq 'Execution error'
}
if ($invalidReport) {
    Write-Error 'Delegate produced no valid final report; treating the run as failed.' -ErrorAction Continue
    if (-not (Test-Path -LiteralPath $ReportFile) -or (Get-Item -LiteralPath $ReportFile -ErrorAction SilentlyContinue).Length -eq 0) {
        Set-Content -LiteralPath $ReportFile -Encoding UTF8 -Value 'Delegate produced no valid final report. Inspect stdout/stderr logs.'
    }
    $ExitCode = 1
}

$HeadSha = ''
$Commits = ''
if ($Worktree -and (Test-Path -LiteralPath $WorktreeDir)) {
    if (-not $NoAutoCommit) {
        & git -C $WorktreeDir add -A *> $null
        & git -C $WorktreeDir diff --cached --quiet
        if ($LASTEXITCODE -ne 0) {
            & git -C $WorktreeDir commit -q -m "delegate(${Mode}): ${timestamp}" *> $null
            if ($LASTEXITCODE -ne 0) {
                & git -C $WorktreeDir -c user.name=delegate -c user.email=delegate@local commit -q -m "delegate(${Mode}): ${timestamp}" *> $null
            }
        }
    }
    $HeadSha = ((& git -C $WorktreeDir rev-parse HEAD 2>$null) -join '').Trim()
    $Commits = ((& git -C $WorktreeDir rev-list --count "$BaseSha..HEAD" 2>$null) -join '').Trim()
    Add-Content -LiteralPath $ReportFile -Encoding UTF8 -Value @"

## Worktree handoff

- Worktree: $WorktreeDir
- Branch: $Branch
- Base: $BaseSha
- Head: $HeadSha
- Commits: $Commits

Review before merging or discarding. Cleanup is deliberately not automatic.
"@
}

New-Item -ItemType File -Force -Path $DoneFile | Out-Null
$status = [ordered]@{
    status = if ($ExitCode -eq 0) { 'completed' } else { 'failed' }
    exit_code = $ExitCode
    backend = $Backend
    profile = $Profile
    model = $Model
    access = $Access
    project_root = $ProjectRoot
    execution_root = $ExecutionRoot
    report = $ReportFile
    stdout = $StdoutLog
    stderr = $StderrLog
    worktree = $WorktreeDir
    branch = $Branch
}
$status | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatusFile -Encoding UTF8

if ($Json) { $status | ConvertTo-Json -Depth 5 }
else {
    Write-Host "Delegate $Mode $(if ($ExitCode -eq 0) { 'completed' } else { 'failed' })."
    Write-Host "Report: $ReportFile"
    Write-Host "Status: $StatusFile"
    if ($Worktree) {
        Write-Host "Worktree: $WorktreeDir"
        Write-Host "Branch: $Branch"
        Write-Host "Review: git -C `"$ProjectRoot`" log --stat $BaseSha..$Branch"
        Write-Host "Merge:  git -C `"$ProjectRoot`" merge --no-ff $Branch"
        Write-Host "Clean:  git -C `"$ProjectRoot`" worktree remove `"$WorktreeDir`" && git -C `"$ProjectRoot`" branch -d $Branch"
    }
}

try { [Console]::Beep(880, 120) } catch {}
exit $ExitCode
