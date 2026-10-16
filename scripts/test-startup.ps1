# Isolated integration check. Reuses installed dependencies through junctions; does not reset the main DB.
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach ($port in @(8000,5173)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "端口 $port 已占用，请停止开发服务后再测试。" }
}
$run = Join-Path $root ('var\startup-review-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $run | Out-Null
$files = & git -c core.quotepath=false -c "safe.directory=$($root.Replace('\','/'))" -C $root ls-files --cached --others --exclude-standard
if ($LASTEXITCODE -ne 0) { throw '源码清单读取失败' }
$python = Join-Path $root '.venv\Scripts\python.exe'
$shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$savedDb = $env:FINAGENT_DB_PATH
$savedPythonPath = $env:PYTHONPATH
$savedKey = $env:MODEL_API_KEY
$originalSearchPath = $env:PATH
$env:MODEL_API_KEY = 'startup-test-placeholder-no-requests'
$results = @()
try {
    foreach ($scenario in @('space-path-and-upgrade','frontend-launch-failure')) {
        $case = Join-Path $run ("项目 with spaces - " + $scenario)
        foreach ($relative in ($files | Select-Object -Unique)) {
            if ($relative -notmatch '^(backend|frontend|scripts)/') { continue }
            $source = Join-Path $root $relative
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
            $target = Join-Path $case $relative
            New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
            Copy-Item -LiteralPath $source -Destination $target
        }
        New-Item -ItemType Junction -Path (Join-Path $case '.venv') -Target (Join-Path $root '.venv') | Out-Null
        New-Item -ItemType Junction -Path (Join-Path $case 'frontend\node_modules') -Target (Join-Path $root 'frontend\node_modules') | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $case 'var\logs') | Out-Null
        $env:PYTHONPATH = Join-Path $root 'backend'
        $env:FINAGENT_DB_PATH = Join-Path $case 'var\finagent.sqlite3'
        & $python -m app.data.cli reset --date 2026-09-24
        if ($LASTEXITCODE -ne 0) { throw '隔离数据初始化失败' }
        # Downgrade a disposable fixture to M3, preserving seeded accounts and setting.
        $before = & $python (Join-Path $root 'scripts\startup-fixture.py') legacy
        if ($LASTEXITCODE -ne 0) { throw '旧库夹具准备失败' }
        if ($scenario -eq 'frontend-launch-failure') {
            # An invalid local Node executable causes Start-Process to fail after the backend starts.
            $fakeRuntime = Join-Path $case 'invalid-node'
            New-Item -ItemType Directory -Path $fakeRuntime | Out-Null
            New-Item -ItemType File -Path (Join-Path $fakeRuntime 'node.exe') | Out-Null
            $env:PATH = $fakeRuntime + ';' + $originalSearchPath
        }
        $launchMode = if ($scenario -eq 'space-path-and-upgrade') { 'live' } else { 'mock' }
        $launcher = $null
        $result = @{scenario=$scenario;passed=$false;case=$case}
        $results += $result
        try {
            $launcher = Start-Process -FilePath $shell -ArgumentList @('-NoProfile','-File',('"' + (Join-Path $case 'scripts\dev.ps1') + '"'),'-Mode',$launchMode) -WorkingDirectory $case -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $case 'launcher.out.log') -RedirectStandardError (Join-Path $case 'launcher.err.log')
            $env:PATH = $originalSearchPath
            if ($scenario -eq 'space-path-and-upgrade') {
                $ready = $false
                for ($i=0; $i -lt 80; $i++) {
                    $launcher.Refresh()
                    if ($launcher.HasExited) { throw '含空格目录启动失败，查看该夹具 launcher.err.log' }
                    try {
                        $api = Invoke-WebRequest 'http://127.0.0.1:8000/api/demo/info' -UseBasicParsing -TimeoutSec 1
                        $ui = Invoke-WebRequest 'http://127.0.0.1:5173' -UseBasicParsing -TimeoutSec 1
                        if ($api.StatusCode -eq 200 -and $ui.StatusCode -eq 200) { $ready=$true; break }
                    } catch { Start-Sleep -Milliseconds 300 }
                }
                if (-not $ready) { throw '含空格目录服务未就绪' }
                if (($api.Content | ConvertFrom-Json).data.mode -ne 'live') { throw 'live 预检未保持模式' }
                $after = & $python (Join-Path $root 'scripts\startup-fixture.py') snapshot
                if ($LASTEXITCODE -ne 0 -or $before -ne $after) { throw '启动未升级旧库或改变了已有数据' }
                $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.Id)")
                $front = @($children | Where-Object Name -eq 'node.exe')
                if ($front.Count -ne 1) { throw '无法识别本测试前端进程' }
                Stop-Process -Id $front[0].ProcessId -Force
            }
            if (-not $launcher.WaitForExit(15000)) { throw '服务失败后启动器没有及时退出' }
            Start-Sleep -Milliseconds 500
            $orphans = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.Id)")
            if ($orphans.Count) { throw '启动失败遗留了子进程' }
            foreach ($port in @(8000,5173)) {
                if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "测试服务未释放端口 $port" }
            }
            $result.passed = $true
            Write-Host "PASS: $scenario"
        } finally {
            if ($launcher) {
                # Kill only this fixture's owned child processes if a check failed.
                Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.Id)" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                $launcher.Refresh()
                if (-not $launcher.HasExited) { Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue }
            }
        }
    }
} finally {
    $env:FINAGENT_DB_PATH = $savedDb
    $env:PYTHONPATH = $savedPythonPath
    $env:MODEL_API_KEY = $savedKey
    $env:PATH = $originalSearchPath
    $results | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $run 'results.json')
    Write-Host "启动检查证据：$run"
}
