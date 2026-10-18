$projectRoot = Split-Path -Parent $PSScriptRoot
$directory = Join-Path $projectRoot 'var'
New-Item -ItemType Directory -Force -Path $directory | Out-Null
$target = Join-Path $directory 'operator.key'
if (-not (Test-Path -LiteralPath $target)) {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    [System.IO.File]::WriteAllText($target, [Convert]::ToBase64String($bytes))
}
Write-Host 'LOCAL SIMULATED OPERATOR - cannot authorize user banking operations'
Get-Content -LiteralPath $target
