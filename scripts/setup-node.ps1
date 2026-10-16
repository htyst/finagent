param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$toolsRoot = Join-Path $projectRoot 'var\tools'
New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
# Official portable LTS runtime. This script never edits PATH or installs globally.
$releaseBase = 'https://nodejs.org/dist/latest-v24.x/'
$checksums = (Invoke-WebRequest -Uri ($releaseBase + 'SHASUMS256.txt') -UseBasicParsing).Content
$match = [regex]::Match($checksums, '(?m)^([0-9a-f]{64})\s+(node-v24\.\d+\.\d+-win-x64\.zip)\s*$')
if (-not $match.Success) { throw 'Official Node 24 Windows archive checksum was not found' }
$expectedHash = $match.Groups[1].Value
$filename = $match.Groups[2].Value
$archivePath = Join-Path $toolsRoot $filename
$runtimeRoot = Join-Path $toolsRoot ([IO.Path]::GetFileNameWithoutExtension($filename))
if (-not (Test-Path -LiteralPath $archivePath)) {
    Invoke-WebRequest -Uri ($releaseBase + $filename) -OutFile $archivePath -UseBasicParsing
}
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'Node archive checksum mismatch; refusing extraction'
}
if (-not (Test-Path -LiteralPath $runtimeRoot)) { Expand-Archive -LiteralPath $archivePath -DestinationPath $toolsRoot }
$node = Join-Path $runtimeRoot 'node.exe'
$npm = Join-Path $runtimeRoot 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $npm)) { throw 'Portable Node installation is incomplete' }
& $node --version
$savedNodePath = $env:PATH
try {
    $env:PATH = $runtimeRoot + ';' + $env:PATH
    & $node $npm ci --prefix (Join-Path $projectRoot 'frontend')
    if ($LASTEXITCODE -ne 0) { throw 'Locked frontend installation failed' }
} finally { $env:PATH = $savedNodePath }
Write-Output ('Project Node: ' + $node)
