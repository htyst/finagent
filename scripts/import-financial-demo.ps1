param()
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONIOENCODING = 'utf-8'
& (Join-Path $root '.venv\Scripts\python.exe') -B (Join-Path $PSScriptRoot 'install-financial-demo.py')
if ($LASTEXITCODE -ne 0) { throw '年度案例导入失败；请保留日志和已有数据库。' }
