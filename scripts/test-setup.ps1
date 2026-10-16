param([Parameter(Mandatory)][string]$Python)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ensure-venv.ps1')
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$testRoot = Join-Path $root ('var\setup-test-' + [guid]::NewGuid().ToString('N'))
$oldScripts = Join-Path $testRoot '.venv\Scripts'
New-Item -ItemType Directory -Force -Path $oldScripts | Out-Null
# Use a real venv launcher with a missing base installation, as in a copied
# Windows project. An arbitrary non-executable file can invoke OS UI handlers.
$launcher = & $Python -c 'import pathlib, venv; print(pathlib.Path(venv.__file__).parent / ''scripts'' / ''nt'' / ''python.exe'')'
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $launcher)) { throw '未找到 venv 启动器' }
Copy-Item -LiteralPath $launcher -Destination (Join-Path $oldScripts 'python.exe')
Set-Content -LiteralPath (Join-Path $testRoot '.venv\pyvenv.cfg') -Value "home = $testRoot\missing-python"
Set-Content -LiteralPath (Join-Path $testRoot '.venv\sentinel.txt') -Value 'keep old files' -NoNewline

$rejected = $false
try { Initialize-ProjectVenv -ProjectRoot $testRoot -Python (Join-Path $testRoot 'missing.exe') | Out-Null }
catch { $rejected = $true }
if (-not $rejected -or -not (Test-Path -LiteralPath (Join-Path $testRoot '.venv\sentinel.txt'))) {
    throw '无效基础解释器不应修改旧环境'
}
$actual = Initialize-ProjectVenv -ProjectRoot $testRoot -Python $Python
if (-not (Test-SupportedPython -Command $actual -RequireVenv)) { throw '未恢复有效解释器' }
$backups = @(Get-ChildItem -LiteralPath (Join-Path $testRoot 'var') -Directory -Filter 'venv-backup-*')
if ($backups.Count -ne 1 -or (Get-Content -LiteralPath (Join-Path $backups[0].FullName 'sentinel.txt') -Raw) -ne 'keep old files') {
    throw '旧环境备份不完整'
}
$configBefore = Get-Content -LiteralPath (Join-Path $testRoot '.venv\pyvenv.cfg') -Raw
$again = Initialize-ProjectVenv -ProjectRoot $testRoot
if ($again -ne $actual -or (Get-Content -LiteralPath (Join-Path $testRoot '.venv\pyvenv.cfg') -Raw) -ne $configBefore) {
    throw '有效环境不应重复重建'
}
Set-Content -LiteralPath (Join-Path $testRoot '.venv\override-sentinel.txt') -Value 'keep override backup'
$overrideRejected = $false
try { Initialize-ProjectVenv -ProjectRoot $testRoot -Python (Join-Path $testRoot 'missing.exe') | Out-Null }
catch { $overrideRejected = $true }
if (-not $overrideRejected -or -not (Test-Path -LiteralPath (Join-Path $testRoot '.venv\override-sentinel.txt'))) {
    throw '无效的 -Python 不应修改有效环境'
}
$overridden = Initialize-ProjectVenv -ProjectRoot $testRoot -Python $Python
$allBackups = @(Get-ChildItem -LiteralPath (Join-Path $testRoot 'var') -Directory -Filter 'venv-backup-*')
if (-not (Test-SupportedPython -Command $overridden -RequireVenv) -or $allBackups.Count -ne 2 -or
    @($allBackups | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'override-sentinel.txt') }).Count -ne 1) {
    throw '指定 -Python 应重建并备份原有的有效环境'
}
Write-Host '安装环境回归通过：无效基础解释器保留原环境、损坏环境备份重建、有效环境复用、指定 Python 强制重建。'
Write-Host "检查证据保留于：$testRoot"
