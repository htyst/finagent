param([ValidateSet('mock','live')][string]$Mode = 'mock')
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONPYCACHEPREFIX = Join-Path $root 'var\cache\python'
$python = Join-Path $root '.venv\Scripts\python.exe'
$vite = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'
. (Join-Path $PSScriptRoot 'ensure-venv.ps1')
if (-not (Test-SupportedPython -Command $python -RequireVenv)) { throw '项目 Python 环境不可运行，请先运行 .\scripts\setup.ps1 修复。' }
if (-not (Test-Path -LiteralPath $vite)) { throw '前端依赖缺失，请先运行 .\scripts\setup.ps1' }
. (Join-Path $PSScriptRoot 'resolve-node.ps1')
$node = Get-ProjectNode -ProjectRoot $root
if (-not (Test-Path -LiteralPath (Join-Path $root 'var\finagent.sqlite3'))) { throw '尚未初始化演示数据，请运行 .\scripts\reset-demo.ps1；已有数据时不要重置。' }
foreach ($port in @(8000,5173)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "端口 $port 已被占用，请先停止之前的开发服务。"
    }
}
$logDir = Join-Path $root 'var\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:PYTHONPATH = Join-Path $root 'backend'
& $python -c 'import uvicorn, fastapi, sqlalchemy, alembic, httpx' *> $null
if ($LASTEXITCODE -ne 0) { throw '后端依赖不完整，请运行 .\scripts\setup.ps1。' }
if ($Mode -eq 'live') {
    & $python -m app.agent.check_config
    if ($LASTEXITCODE -ne 0) { throw 'live 模式需要在项目本地 .env 配置 MODEL_API_KEY' }
}
$env:FINAGENT_DB_PATH = Join-Path $root 'var\finagent.sqlite3'
$env:FINAGENT_MODE = $Mode
$env:FINAGENT_ALLOWED_ORIGIN = 'http://127.0.0.1:5173'
# Upgrade schema only. Never call reset from the normal startup path.
& $python -m app.data.cli migrate
if ($LASTEXITCODE -ne 0) { throw '数据库升级失败，服务未启动；请保留数据并检查上方错误。' }
$backend = $null
$frontend = $null
try {
    $backend = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000') -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'backend.out.log') -RedirectStandardError (Join-Path $logDir 'backend.err.log')
    $frontend = Start-Process -FilePath $node -ArgumentList @(('"' + $vite + '"'),'--host','127.0.0.1','--port','5173','--strictPort') -WorkingDirectory (Join-Path $root 'frontend') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'frontend.out.log') -RedirectStandardError (Join-Path $logDir 'frontend.err.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $backend.Refresh(); $frontend.Refresh()
        if ($backend.HasExited -or $frontend.HasExited) { throw '服务启动失败，请查看 var/logs/ 中的错误日志。' }
        try {
            $api = Invoke-WebRequest 'http://127.0.0.1:8000/api/demo/info' -UseBasicParsing -TimeoutSec 2
            $ui = Invoke-WebRequest 'http://127.0.0.1:5173' -UseBasicParsing -TimeoutSec 2
            if ($api.StatusCode -eq 200 -and $ui.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 300 }
    }
    if (-not $ready) { throw '服务启动超时，请查看 var/logs/；本次启动的进程将退出。' }
    Write-Host "FinAgent $Mode 模式已就绪： http://127.0.0.1:5173"
    Write-Host '按 Ctrl+C 停止本脚本启动的服务。'
    while (-not $backend.HasExited -and -not $frontend.HasExited) { Start-Sleep -Seconds 1; $backend.Refresh(); $frontend.Refresh() }
    throw '服务意外退出；请查看 var/logs/'
} finally {
    foreach ($process in @($backend,$frontend)) {
        if ($process) { $process.Refresh(); if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } }
    }
}
