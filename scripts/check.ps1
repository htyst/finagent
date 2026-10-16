param([ValidateSet('M1','M2','M3','M4','M5','M6','All')][string]$Stage = 'M1')
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
# Deterministic checks must stay mock even after a live script in this terminal.
$env:FINAGENT_MODE = 'mock'
$all = $Stage -eq 'All'
if ($all) { $Stage = 'M6' }
if ($Stage -eq 'M6' -and $env:FINAGENT_M6_CASE) { throw '完整 M6 验收请先清除 FINAGENT_M6_CASE 单项筛选' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 .\scripts\setup.ps1' }
$env:PYTHONPATH = Join-Path $root 'backend'
$env:FINAGENT_DB_PATH = Join-Path $root 'var\finagent.sqlite3'
Write-Host "[$Stage] 后端测试"
$tests = @((Join-Path $root 'backend\tests\test_m1.py'))
if ($Stage -in @('M2','M3','M4','M5','M6')) { $tests += (Join-Path $root 'backend\tests\test_m2.py') }
if ($Stage -in @('M3','M4','M5','M6')) { $tests += (Join-Path $root 'backend\tests\test_m3.py') }
if ($Stage -in @('M4','M5','M6')) { $tests += (Join-Path $root 'backend\tests\test_m4.py') }
if ($Stage -in @('M5','M6')) { $tests += (Join-Path $root 'backend\tests\test_m5.py') }
if ($Stage -eq 'M6') { $tests += (Join-Path $root 'backend\tests\test_m6.py') }
if ($all) { $tests = @((Join-Path $root 'backend\tests')) }
& $python -m pytest @tests -q
if ($LASTEXITCODE -ne 0) { throw '后端测试失败' }
Write-Host "[$Stage] 前端类型检查与构建"
Push-Location (Join-Path $root 'frontend')
try {
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
} finally { Pop-Location }
Write-Host "[$Stage] 已实现的确定性检查通过"

if ($Stage -eq 'M6') {
    npm.cmd run browser:regression --prefix (Join-Path $root 'frontend')
    if ($LASTEXITCODE -ne 0) { throw '受控响应浏览器回归失败' }
    foreach ($script in @('browser-m3.ps1','browser-m4.ps1','browser-m5.ps1','browser-m6.ps1')) {
        & (Join-Path $PSScriptRoot $script)
        if ($LASTEXITCODE -ne 0) { throw "$script 浏览器验收失败" }
    }
    Write-Host '[M6] 全部指定浏览器流程通过'
}

if ($all) {
    & (Join-Path $PSScriptRoot 'test-startup.ps1')
    if ($LASTEXITCODE -ne 0) { throw '启动集成检查失败' }
}
