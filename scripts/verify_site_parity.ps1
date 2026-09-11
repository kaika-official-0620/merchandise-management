param()
$ErrorActionPreference = 'Stop'
$siteRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workspaceRoot = [IO.Path]::GetFullPath((Join-Path $siteRoot '..\..'))
$runtimePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$previewDependencies = Join-Path $workspaceRoot 'kaika-mobile\.preview-python-deps'
if (!(Test-Path -LiteralPath $runtimePython) -or !(Test-Path -LiteralPath $previewDependencies)) {
    throw 'The local Python runtime or PC preview dependencies are missing.'
}
$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONPATH = $previewDependencies
    $env:PYTHONUTF8 = '1'
    $runOutput = & $runtimePython -u (Join-Path $PSScriptRoot 'verify_site_parity.py')
    $runExit = $LASTEXITCODE
    $jsonLine = $runOutput | Where-Object { $_.StartsWith('SITE_VERIFICATION_JSON=') } | Select-Object -Last 1
    if (!$jsonLine) { throw 'The isolated verifier did not produce a report.' }
    $report = $jsonLine.Substring(23) | ConvertFrom-Json
    $artifactDir = Join-Path $siteRoot 'docs\verification-artifacts'
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
    foreach ($artifact in $report.artifacts) {
        if ($artifact.fictional_data_only -ne $true -or $artifact.name -notmatch '^[a-z0-9-]+\.(html|csv|pdf)$') {
            throw 'The verifier returned an unexpected artifact name.'
        }
        [IO.File]::WriteAllBytes((Join-Path $artifactDir $artifact.name), [Convert]::FromBase64String($artifact.base64))
    }
    $report.PSObject.Properties.Remove('artifacts')
    [IO.File]::WriteAllText((Join-Path $siteRoot 'docs\site-verification.json'),
        ($report | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    $report.summary | ConvertTo-Json -Depth 5
    Write-Output "Report: $(Join-Path $siteRoot 'docs\site-verification.json')"
    Write-Output "Fictional artifacts: $artifactDir"
    exit $runExit
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONUTF8 = $previousPythonUtf8
}
