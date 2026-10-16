param(
    [Parameter(Mandatory=$true)][ValidateSet('mock','live')][string]$Mode,
    [Parameter(Mandatory=$true)][ValidateSet('dev','holdout')][string]$Split,
    [ValidateSet('full','key')][string]$Suite = 'full',
    [ValidateRange(1,10)][int]$Repeat = 1
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 .\scripts\setup.ps1' }
$env:PYTHONPATH = Join-Path $root 'backend'
& $python -m app.agent.evaluate --mode $Mode --split $Split --suite $Suite --repeat $Repeat
if ($LASTEXITCODE -ne 0) { throw "$Mode/$Split 评估存在失败；详见 var/evals/ 报告" }
