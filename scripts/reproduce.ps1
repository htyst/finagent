param([switch]$Browser)
$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$copy=Join-Path $root "var\repro\$stamp\finagent"
New-Item -ItemType Directory -Force -Path $copy | Out-Null
$files = @(& git -c core.quotepath=false -c "safe.directory=$($root.Replace('\','/'))" -C $root ls-files --cached --others --exclude-standard) | Select-Object -Unique
if ($LASTEXITCODE -ne 0) { throw 'Cannot enumerate source files' }
$manifest=@()
foreach ($relative in $files) {
    if ($relative -match '(^|/)(var|node_modules|.venv|.git)/' -or $relative -eq '.env') { throw 'Private/generated source in copy list' }
    $source=Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
    $target=Join-Path $copy $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item -LiteralPath $source -Destination $target
    $manifest += @{path=$relative;sha256=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash}
}
$basePython=& (Join-Path $root '.venv\Scripts\python.exe') -c 'import sys; print(sys._base_executable)'
$report=@{source=$root;copy=$copy;started=(Get-Date -Format o);source_files=$manifest;passed=$false;browser_requested=[bool]$Browser}
$oldDb=$env:FINAGENT_DB_PATH
$oldPath=$env:PYTHONPATH
$oldMode=$env:FINAGENT_MODE
$oldKey=$env:MODEL_API_KEY
$env:MODEL_API_KEY=$null
$log=Join-Path (Split-Path $copy) 'reproduction.log'
Start-Transcript -Path $log | Out-Null
try {
    & (Join-Path $copy 'scripts\setup.ps1') -Python $basePython
    & (Join-Path $copy 'scripts\reset-demo.ps1') -Date '2026-09-24'
    $env:PYTHONPATH=Join-Path $copy 'backend'
    & (Join-Path $copy '.venv\Scripts\python.exe') -m pytest (Join-Path $copy 'backend\tests') -q
    if ($LASTEXITCODE -ne 0) { throw 'Fresh backend tests failed' }
    & npm.cmd run build --prefix (Join-Path $copy 'frontend')
    if ($LASTEXITCODE -ne 0) { throw 'Fresh build failed' }
    if ($Browser) { & (Join-Path $copy 'scripts\browser-m3.ps1') }
    $report.passed=$true
} catch { $report.error=$_.Exception.Message; throw }
finally {
    $report.finished=Get-Date -Format o
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path (Split-Path $copy) 'result.json')
    Stop-Transcript | Out-Null
    $env:FINAGENT_DB_PATH=$oldDb
    $env:PYTHONPATH=$oldPath
    $env:FINAGENT_MODE=$oldMode
    $env:MODEL_API_KEY=$oldKey
}
Write-Host "Clean reproduction passed: $copy"
