param()
$ErrorActionPreference = 'Stop'
$siteRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workspaceRoot = [IO.Path]::GetFullPath((Join-Path $siteRoot '..\..'))
$runtimePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$previewDependencies = Join-Path $workspaceRoot 'kaika-mobile\.preview-python-deps'
if (!(Test-Path -LiteralPath $runtimePython) -or !(Test-Path -LiteralPath $previewDependencies)) { throw 'Local preview runtime is missing.' }
$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONPATH = $previewDependencies
    $env:PYTHONUTF8 = '1'
    $runOutput = & $runtimePython -u (Join-Path $PSScriptRoot 'verify_invoice_tax.py')
    $runExit = $LASTEXITCODE
    $jsonLine = $runOutput | Where-Object { $_.StartsWith('INVOICE_TAX_JSON=') } | Select-Object -Last 1
    if (!$jsonLine) { throw 'The isolated tax verifier did not produce a report.' }
    $report = $jsonLine.Substring(17) | ConvertFrom-Json
    if ($report.fictional_data_only -ne $true) { throw 'Only fictional artifacts may be exported.' }
    $artifactDir = Join-Path $siteRoot 'docs\verification-artifacts'
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
    foreach ($artifact in $report.artifacts) {
        if ($artifact.name -notmatch '^tax-[a-z0-9-]+\.(html|csv)$') { throw 'Unexpected artifact name.' }
        [IO.File]::WriteAllBytes((Join-Path $artifactDir $artifact.name), [Convert]::FromBase64String($artifact.base64))
    }
    $report.PSObject.Properties.Remove('artifacts')
    [IO.File]::WriteAllText((Join-Path $siteRoot 'docs\invoice-tax-verification.json'), ($report | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    $report.summary | ConvertTo-Json
    exit $runExit
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONUTF8 = $previousPythonUtf8
}
