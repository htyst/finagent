$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$vite = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $vite)) {
    throw '请先运行 .\scripts\setup.ps1'
}
foreach ($port in @(8000, 5173)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "端口 $port 已被占用；请先停止正在运行的开发服务"
    }
}
$var = [System.IO.Path]::GetFullPath((Join-Path $root 'var'))
$database = [System.IO.Path]::GetFullPath((Join-Path $var 'm3-browser.sqlite3'))
if (-not $database.StartsWith($var + [System.IO.Path]::DirectorySeparatorChar,
                              [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '浏览器测试数据库路径不在项目 var/ 内'
}
$env:PYTHONPATH = Join-Path $root 'backend'
$env:FINAGENT_DB_PATH = $database
$env:FINAGENT_MODE = 'mock'
$env:FINAGENT_ALLOWED_ORIGIN = 'http://127.0.0.1:5173'
& $python -m app.data.cli reset --date 2026-09-24
if ($LASTEXITCODE -ne 0) { throw '初始化隔离演示数据库失败' }

$logDir = Join-Path $var 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$node = (Get-Command node).Source
$backend = $null
$frontend = $null
try {
    $backend = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'm3-browser-backend.out.log') -RedirectStandardError (Join-Path $logDir 'm3-browser-backend.err.log')
    $frontend = Start-Process -FilePath $node -ArgumentList @(('"' + $vite + '"'),'--host','127.0.0.1','--port','5173','--strictPort') -WorkingDirectory (Join-Path $root 'frontend') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'm3-browser-frontend.out.log') -RedirectStandardError (Join-Path $logDir 'm3-browser-frontend.err.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($backend.HasExited -or $frontend.HasExited) { throw '测试服务提前退出，请查看 var/logs/' }
        try {
            $api = Invoke-WebRequest 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 2
            $ui = Invoke-WebRequest 'http://127.0.0.1:5173' -UseBasicParsing -TimeoutSec 2
            if ($api.StatusCode -eq 200 -and $ui.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw '测试服务启动超时，请查看 var/logs/' }
    Push-Location (Join-Path $root 'frontend')
    try {
        npm.cmd run browser:smoke
        if ($LASTEXITCODE -ne 0) { throw 'M1–M2 浏览器回归失败' }
        npm.cmd run browser:m3
        if ($LASTEXITCODE -ne 0) { throw 'M3 浏览器流程失败' }
    } finally { Pop-Location }
} finally {
    foreach ($process in @($backend, $frontend)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
