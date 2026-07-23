$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $Root

$parseErrors = @()
Get-ChildItem -LiteralPath (Join-Path $Root 'bin') -Filter '*.ps1' -File |
    ForEach-Object {
        $tokens = $null
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $_.FullName,
            [ref]$tokens,
            [ref]$errors
        ) | Out-Null
        $parseErrors += $errors
    }
if ($parseErrors.Count) {
    $parseErrors | ForEach-Object {
        Write-Error "$($_.Extent.File):$($_.Extent.StartLineNumber): $($_.Message)"
    }
    throw 'PowerShell syntax validation failed'
}

python (Join-Path $Root 'tools\render_config.py') --check
if ($LASTEXITCODE -ne 0) { throw 'generated/ is stale - run tools/render_config.py' }

python (Join-Path $Root 'tools\verify_kit.py')
if ($LASTEXITCODE -ne 0) { throw 'verify_kit failed' }

python -m unittest discover -s (Join-Path $Root 'tests') -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw 'unit tests failed' }

'windows smoke tests passed'
Pop-Location
