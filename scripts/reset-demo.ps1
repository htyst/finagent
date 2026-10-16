param([string]$Date = '2026-09-24')
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 .\scripts\setup.ps1' }
$database = [System.IO.Path]::GetFullPath((Join-Path $root 'var\finagent.sqlite3'))
$var = [System.IO.Path]::GetFullPath((Join-Path $root 'var'))
if (-not $database.StartsWith($var + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) { throw '数据库路径不在项目 var/ 内' }
$env:FINAGENT_DB_PATH = $database
$env:PYTHONPATH = Join-Path $root 'backend'
& $python -m app.data.cli reset --date $Date
if ($LASTEXITCODE -ne 0) { throw '重置失败' }
