param([string]$Python = '', [string]$Node = 'node', [switch]$SkipBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
. (Join-Path $PSScriptRoot 'resolve-node.ps1')
if (-not $Node -or $Node -eq 'node') { $Node = Get-ProjectNode -ProjectRoot $projectRoot }
$runId = [guid]::NewGuid().ToString('N')
$runRoot = Join-Path $projectRoot ('var\TEST-ONLY\p0-' + $runId)
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$backendProcess = $null
$frontendProcess = $null
$saved = @{}
$names = @('PYTHONPATH','PYTHONIOENCODING','FINAGENT_DB_PATH','FINAGENT_MODE','FINAGENT_ALLOWED_ORIGIN','FINAGENT_API_PROXY_TARGET','FINAGENT_E2E_BASE_URL','FINAGENT_SCHEDULER_ENABLED','FINAGENT_SUBSCRIPTIONS_ENABLED','FINAGENT_BIRTHDAYS_ENABLED')
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$result = @{ started = [DateTimeOffset]::UtcNow.ToString('o'); status = 'running'; passed = @(); failure = $null }
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'backend'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:FINAGENT_DB_PATH = Join-Path $runRoot 'browser.sqlite3'
    $env:FINAGENT_MODE = 'mock'
    $env:FINAGENT_ALLOWED_ORIGIN = 'http://127.0.0.1:15173'
    $env:FINAGENT_API_PROXY_TARGET = 'http://127.0.0.1:18000'
    $env:FINAGENT_E2E_BASE_URL = 'http://127.0.0.1:15173'
    $env:FINAGENT_SCHEDULER_ENABLED = '0'
    $env:FINAGENT_SUBSCRIPTIONS_ENABLED = '0'
    $env:FINAGENT_BIRTHDAYS_ENABLED = '0'
    & $Python -c 'import fastapi, sqlalchemy, alembic, pytest, httpx'
    if ($LASTEXITCODE -ne 0) { throw '后端依赖缺失。请先完成 setup.ps1；本脚本不会安装或下载依赖。' }
    & $Python -m pytest (Join-Path $projectRoot 'backend\tests') -q --basetemp (Join-Path $runRoot 'pytest') --junitxml (Join-Path $runRoot 'backend.xml')
    if ($LASTEXITCODE -ne 0) { throw '后端回归失败，保留本次 XML 和隔离数据库。' }
    $result.passed += 'backend-pytest'
    Push-Location (Join-Path $projectRoot 'frontend')
    try {
        & $Node tests/api-regression.cjs
        if ($LASTEXITCODE -ne 0) { throw '前端API回归失败' }
        $result.passed += 'frontend-api'
        & $Node node_modules/typescript/bin/tsc -b
        if ($LASTEXITCODE -ne 0) { throw 'TypeScript完整类型检查失败' }
        & $Node node_modules/vite/bin/vite.js build
        if ($LASTEXITCODE -ne 0) { throw '前端生产构建失败' }
        $result.passed += 'frontend-build'
    } finally { Pop-Location }
    if (-not $SkipBrowser) {
        foreach ($port in @(18000,15173)) {
            if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "测试端口 $port 已占用，未关闭任何已有服务。" }
        }
        & $Python -m app.data.cli reset --date 2026-10-14
        if ($LASTEXITCODE -ne 0) { throw '隔离数据库迁移或初始化失败' }
        $backendProcess = Start-Process -FilePath $Python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18000') -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'backend.out.log') -RedirectStandardError (Join-Path $runRoot 'backend.err.log')
        $frontendProcess = Start-Process -FilePath $Node -ArgumentList @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','15173','--strictPort') -WorkingDirectory (Join-Path $projectRoot 'frontend') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'frontend.out.log') -RedirectStandardError (Join-Path $runRoot 'frontend.err.log')
        $ready = $false
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            try {
                $response = Invoke-RestMethod -Uri ($env:FINAGENT_E2E_BASE_URL + '/api/health') -TimeoutSec 1
                if ($response.data.test_only) { $ready = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw '隔离服务未就绪，详情见本次日志' }
        Push-Location (Join-Path $projectRoot 'frontend')
        try {
            & $Node tests/p0-browser.mjs
            if ($LASTEXITCODE -ne 0) { throw '浏览器回归失败' }
        } finally { Pop-Location }
        $result.passed += 'browser'
    }
    $result.status = if ($SkipBrowser) { 'partial-browser-skipped' } else { 'passed' }
} catch {
    $result.status = 'failed'
    $result.failure = $_.Exception.Message
    throw
} finally {
    foreach ($process in @($backendProcess, $frontendProcess)) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    $result.finished = [DateTimeOffset]::UtcNow.ToString('o')
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot 'result.json') -Encoding UTF8
    Write-Host "本次验收证据：$runRoot"
}
