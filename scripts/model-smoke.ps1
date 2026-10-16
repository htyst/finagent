$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 .\scripts\setup.ps1' }
$env:PYTHONPATH = Join-Path $root 'backend'
& $python -m app.agent.smoke
if ($LASTEXITCODE -ne 0) { throw '模型小试验未通过；详见上方错误' }
