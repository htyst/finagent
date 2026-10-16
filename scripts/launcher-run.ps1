param([ValidateSet('mock','live')][string]$Mode = 'mock')
$ErrorActionPreference = 'Stop'
try {
    & (Join-Path $PSScriptRoot 'dev.ps1') -Mode $Mode
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
