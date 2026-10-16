<#
.SYNOPSIS
    FinAgent 一键测试运行器：分层执行、计时、收集证据、生成可视化报告。

.DESCRIPTION
    把散落在各个脚本里的确定性检查串成一条可复现的流水线，并产出：
      var\testplan\latest\dashboard.html  可视化仪表盘（单文件，可转发）
      var\testplan\latest\results.json    机器可读结果（可做趋势对比）
      var\testplan\latest\summary.md      纯文本汇总
      var\testplan\latest\junit-backend.xml
      var\testplan\latest\logs\*.log      每步原始输出

    设计约束：
      * 只用 mock，绝不隐式调用模型（不产生任何 API 费用）。
      * 只读业务数据：后端测试全部使用临时/隔离数据库；本项目业务库只被只读探查。
      * 若项目目录不可写，自动降级到 %USERPROFILE%\finagent-testplan\<时间戳>\ 并打印回填命令。

.PARAMETER Tier
    quick    实测约 7 秒：领域规则 + 会话隔离 + 手算统计 + 限额的冒烟子集
    standard 实测约 29 秒：全部 103 项后端测试 + 前端类型检查与生产构建（推荐）
    full     约 20–30 分钟：standard + 受控响应回归 + M3–M6 浏览器验收 + 启动集成

.PARAMETER OutDir
    报告输出目录。默认 <项目>\var\testplan\latest

.PARAMETER Scope
    All（默认）/ Backend / Frontend —— 只跑其中一部分，便于定位。

.PARAMETER ProjectRoot
    项目根目录。默认由脚本位置推断；用于脚本被移到项目外时的降级运行。

.PARAMETER NoOpen
    不自动打开仪表盘。

.EXAMPLE
    .\scripts\testplan.ps1 -Tier quick
    .\scripts\testplan.ps1 -Tier standard
    .\scripts\testplan.ps1 -Tier full -NoOpen
#>
[CmdletBinding()]
param(
    [ValidateSet('quick', 'standard', 'full')][string]$Tier = 'standard',
    [string]$OutDir,
    [ValidateSet('All', 'Backend', 'Frontend')][string]$Scope = 'All',
    [string]$ProjectRoot,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding } catch { }

# ---------------------------------------------------------------- 定位项目根
function Test-IsProjectRoot([string]$dir) {
    if (-not $dir) { return $false }
    return (Test-Path -LiteralPath (Join-Path $dir 'backend')) -and (Test-Path -LiteralPath (Join-Path $dir 'backend\tests'))
}

# 正常部署时本脚本位于 <项目>\scripts\ 下；被移到别处时依次尝试：
# 显式参数 → 向上逐级查找项目根 → 环境变量。
$resolveRoot = {
    $candidate = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    if (Test-IsProjectRoot $candidate) { return $candidate }
    # 从当前目录向上找，便于把方案放在项目内任意深度或项目外相邻目录
    $probe = (Get-Location).Path
    for ($i = 0; $i -lt 8 -and $probe; $i++) {
        if (Test-IsProjectRoot $probe) { return $probe }
        $parent = Split-Path $probe -Parent
        if (-not $parent -or $parent -eq $probe) { break }
        $probe = $parent
    }
    # 脚本自身向上逐级查找
    $probe = (Resolve-Path $PSScriptRoot).Path
    for ($i = 0; $i -lt 8 -and $probe; $i++) {
        if (Test-IsProjectRoot $probe) { return $probe }
        $parent = Split-Path $probe -Parent
        if (-not $parent -or $parent -eq $probe) { break }
        $probe = $parent
    }
    if (Test-IsProjectRoot $env:FINAGENT_PROJECT_ROOT) { return $env:FINAGENT_PROJECT_ROOT }
    return $null
}

if ($ProjectRoot) {
    $root = (Resolve-Path -LiteralPath $ProjectRoot).Path
} else {
    $root = & $resolveRoot
    if (-not $root) {
        throw '无法确定项目根目录。请在项目内运行，或用 -ProjectRoot 指定（应含 backend\tests\）。'
    }
}
if (-not (Test-IsProjectRoot $root)) { throw "$root 看起来不是 FinAgent 项目根目录（缺少 backend\tests\）" }

$python = Join-Path $root '.venv\Scripts\python.exe'
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'

# tools/ 与 docs/ 优先用项目内的；项目内没有时回落到脚本同级目录，
# 便于在项目目录暂不可写时把本方案放在外部运行。
function Resolve-Asset([string]$relative) {
    $inProject = Join-Path $root $relative
    if (Test-Path -LiteralPath $inProject) { return $inProject }
    $beside = Join-Path (Split-Path $PSScriptRoot -Parent) $relative
    if (Test-Path -LiteralPath $beside) { return $beside }
    return $inProject
}
$toolsDir = Resolve-Asset 'tools'
$docsDir = Resolve-Asset 'docs'

if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到虚拟环境解释器 $python；请先运行 .\scripts\setup.ps1"
}

# 确定性检查必须固定 mock，即使终端之前跑过 live。
$env:FINAGENT_MODE = 'mock'
$env:PYTHONPATH = $backendDir
$env:PYTHONDONTWRITEBYTECODE = '1'

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $OutDir) { $OutDir = Join-Path $root 'var\testplan\latest' }

function Test-Writable([string]$dir) {
    try {
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $probe = Join-Path $dir ('.wprobe-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        [System.IO.File]::WriteAllText($probe, 'x')
        Remove-Item -LiteralPath $probe -Force
        return $true
    } catch { return $false }
}

$degraded = $false
$requestedOutDir = $OutDir
if (-not (Test-Writable $OutDir)) {
    $degraded = $true
    $OutDir = Join-Path (Join-Path $env:USERPROFILE 'finagent-testplan') $stamp
    if (-not (Test-Writable $OutDir)) {
        $OutDir = Join-Path $env:TEMP ('finagent-testplan-' + $stamp)
        if (-not (Test-Writable $OutDir)) { throw '无法创建任何可写的结果目录，请检查磁盘与权限' }
    }
}

$logDir = Join-Path $OutDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 让 pytest / python 的缓存也落在可写位置，避免污染只读的项目树。
$env:PYTHONPYCACHEPREFIX = Join-Path $OutDir 'pycache'
New-Item -ItemType Directory -Force -Path $env:PYTHONPYCACHEPREFIX | Out-Null
$env:TEMP = $OutDir
$env:TMP = $OutDir

# pytest 默认在共享的系统临时目录下建 pytest-of-<用户>\pytest-current（一个重解析点/junction）。
# 该链接一旦残留或指向被删除的目标，pytest 在会话收尾清理时会抛 PermissionError，
# 导致“测试全部通过但退出码为 1”的假失败。把 basetemp 固定到本次运行的可写目录即可规避。
$baseTempDir = Join-Path $OutDir 'pytest-tmp'
New-Item -ItemType Directory -Force -Path $baseTempDir | Out-Null

$junitPath = Join-Path $OutDir 'junit-backend.xml'
$resultsPath = Join-Path $OutDir 'results.json'
$dashboardPath = Join-Path $OutDir 'dashboard.html'
$summaryPath = Join-Path $OutDir 'summary.md'
$factsPath = Join-Path $OutDir 'demo-facts.json'
$distDir = Join-Path $OutDir 'frontend-dist'

# ---------------------------------------------------------------- 输出工具
function Write-Head([string]$text) {
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
}

$steps = New-Object System.Collections.Generic.List[object]

# 统一在函数内判定成败：脚本块通过 throw 表达失败，避免依赖易失的 $LASTEXITCODE。
function Invoke-Step {
    param(
        [string]$Id,
        [string]$Name,
        [string]$LogName,
        [scriptblock]$Body
    )
    Write-Host ''
    Write-Host "[$Id] $Name" -ForegroundColor Yellow
    $logPath = Join-Path $logDir ($LogName + '.log')
    $status = 'passed'
    $detail = ''
    $t0 = Get-Date
    # 原生命令（npm/pytest）会往 stderr 写正常进度；在 Stop 模式下会被误判为异常。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $detail = (& $Body) 2>&1 | Tee-Object -FilePath $logPath -Encoding UTF8 | Out-String
    } catch {
        $status = 'failed'
        $detail = ($detail + "`n[EXCEPTION] " + ($_ | Out-String))
        Add-Content -LiteralPath $logPath -Value ("`n[EXCEPTION] " + ($_ | Out-String)) -Encoding UTF8
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $tail = ($detail -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' | '
    $color = if ($status -eq 'passed') { 'Green' } else { 'Red' }
    Write-Host ("     -> {0}  ({1}s)  {2}" -f $status.ToUpper(), $seconds, $tail) -ForegroundColor $color
    $steps.Add([pscustomobject]@{
            id      = $Id
            name    = $Name
            status  = $status
            seconds = $seconds
            log     = (Split-Path $logPath -Leaf)
            detail  = $tail
        })
}

# 运行原生命令并在非零退出码时抛出，让 Invoke-Step 统一记为失败。
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$WorkDir, [string]$What)
    $prev = Get-Location
    if ($WorkDir) { Set-Location -LiteralPath $WorkDir }
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally { Set-Location -LiteralPath $prev }
    if ($code -ne 0) { throw "$What 失败（退出码 $code）" }
    return $code
}

# ---------------------------------------------------------------- 开始
Write-Head "FinAgent 测试方案  |  档位=$Tier  范围=$Scope"
Write-Host "  项目根目录 : $root"
Write-Host "  结果目录   : $OutDir"
if ($degraded) {
    Write-Host "  [注意] 项目目录当前不可写，结果已降级到用户目录。" -ForegroundColor Magenta
    Write-Host "         修复权限后可用以下命令回填到项目：" -ForegroundColor Magenta
    Write-Host "         Copy-Item -Recurse -Force '$OutDir\*' '$requestedOutDir\'" -ForegroundColor Magenta
}

$tierInfo = @{
    quick    = '实测约 7 秒：领域规则 + 会话隔离 + 手算统计 + 限额'
    standard = '实测约 29 秒：全部 103 项后端测试 + 前端类型检查与生产构建'
    full     = '约 20–30 分钟：+ 受控响应回归 + M3–M6 浏览器验收 + 启动集成'
}[$Tier]
Write-Host "  档位含义   : $tierInfo"
Write-Host "  模型调用   : 无（固定 mock，不产生任何费用）"

# ================================================================ S1 演示库事实探查
if ($Scope -ne 'Frontend') {
    Invoke-Step -Id 'S1' -Name '演示库事实探查（只读，产出人工核对基准）' -LogName 'demo-facts' -Body {
        $probe = Join-Path $toolsDir 'probe_demo_facts.py'
        if (-not (Test-Path -LiteralPath $probe)) { throw "缺少 $probe" }
        $db = Join-Path $root 'var\finagent.sqlite3'
        if (-not (Test-Path -LiteralPath $db)) { throw "缺少演示数据库 $db；请先运行 .\scripts\reset-demo.ps1" }
        $json = & $python $probe $db
        if ($LASTEXITCODE -ne 0) { throw '演示库探查失败' }
        $json | Set-Content -LiteralPath $factsPath -Encoding UTF8
        $json
    }
}

# ================================================================ S2 后端测试
if ($Scope -ne 'Frontend') {
    if ($Tier -eq 'quick') {
        # 冒烟子集：每个方向挑一条最能代表该方向的用例，用 -k 选择器表达以便随测试改名而报错。
        $quickK = @(
            'test_money_and_two_clocks',
            'test_seed_is_reproducible_and_balanced',
            'test_session_owner_scopes',
            'test_spending_hand_calculated',
            'test_daily_limit_and_internal_pair'
        ) -join ' or '
        Invoke-Step -Id 'S2' -Name '后端冒烟子集（金额与时区 / 种子账本 / 会话隔离 / 手算统计 / 限额）' -LogName 'pytest-quick' -Body {
            Invoke-Native -Exe $python -WorkDir $OutDir -What '后端冒烟子集' -Arguments @(
                '-m', 'pytest', (Join-Path $backendDir 'tests'),
                '-q', '-p', 'no:cacheprovider', '--no-header', '-rN',
                ('--basetemp=' + $baseTempDir),
                ('--junit-xml=' + $junitPath),
                '-k', $quickK
            )
        }
    } else {
        Invoke-Step -Id 'S2' -Name '后端全部测试（证据写入 junit-backend.xml；用例数见报告与 junit）' -LogName 'pytest-all' -Body {
            Invoke-Native -Exe $python -WorkDir $OutDir -What '后端测试' -Arguments @(
                '-m', 'pytest', (Join-Path $backendDir 'tests'),
                '-q', '-p', 'no:cacheprovider', '--no-header', '-rN',
                '--durations=15',
                ('--basetemp=' + $baseTempDir),
                ('--junit-xml=' + $junitPath)
            )
        }
    }
}

# ================================================================ S3 前端类型检查
$frontendReady = Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules')
if ($Scope -ne 'Backend') {
    Invoke-Step -Id 'S3' -Name '前端 TypeScript 类型检查（tsc --noEmit，不写文件）' -LogName 'frontend-typecheck' -Body {
        if (-not $frontendReady) { throw '缺少 frontend\node_modules；请先运行 .\scripts\setup.ps1' }
        Invoke-Native -Exe 'npm.cmd' -WorkDir $frontendDir -What '类型检查' -Arguments @('run', 'typecheck')
    }
}

# ================================================================ S4 前端构建（standard 起）
if ($Scope -ne 'Backend' -and $Tier -ne 'quick') {
    Invoke-Step -Id 'S4' -Name '前端生产构建（vite build，产物写到结果目录以不污染项目）' -LogName 'frontend-build' -Body {
        if (-not $frontendReady) { throw '缺少 frontend\node_modules；请先运行 .\scripts\setup.ps1' }
        $vite = Join-Path $frontendDir 'node_modules\vite\bin\vite.js'
        if (-not (Test-Path -LiteralPath $vite)) { throw "缺少 $vite" }
        Invoke-Native -Exe 'node' -WorkDir $frontendDir -What '生产构建' -Arguments @(
            $vite, 'build', '--outDir', $distDir, '--emptyOutDir'
        )
    }
}

# ================================================================ S5 受控响应回归（full）
if ($Tier -eq 'full' -and $Scope -ne 'Backend') {
    Invoke-Step -Id 'S5' -Name '受控响应浏览器回归（前端 6 项竞态）' -LogName 'browser-regression' -Body {
        if (-not $frontendReady) { throw '缺少 frontend\node_modules' }
        Invoke-Native -Exe 'npm.cmd' -WorkDir $frontendDir -What '受控响应回归' -Arguments @('run', 'browser:regression')
    }
}

# ================================================================ S6–S9 页面验收（full）
if ($Tier -eq 'full' -and $Scope -ne 'Frontend') {
    $browserSteps = @(
        @{ Id = 'S6'; Script = 'browser-m3.ps1'; Name = '页面验收 M1–M3（只读 + 表单确认 + 隔离库）' },
        @{ Id = 'S7'; Script = 'browser-m4.ps1'; Name = '页面验收 M4（对话 + 追问 + 页面确认）' },
        @{ Id = 'S8'; Script = 'browser-m5.ps1'; Name = '页面验收 M5（两条跨场景故事）' },
        @{ Id = 'S9'; Script = 'browser-m6.ps1'; Name = '页面验收 M6（网络故障 / 多标签页 / 窄屏）' }
    )
    foreach ($bs in $browserSteps) {
        $scriptPath = Join-Path $root ('scripts\' + $bs.Script)
        if (-not (Test-Path -LiteralPath $scriptPath)) { continue }
        $sId = $bs.Id; $sName = $bs.Name; $sPath = $scriptPath; $sScript = $bs.Script
        Invoke-Step -Id $sId -Name $sName -LogName ([IO.Path]::GetFileNameWithoutExtension($sScript)) -Body {
            Invoke-Native -Exe 'pwsh' -WorkDir $root -What $sScript -Arguments @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $sPath
            )
        }
    }

    $startup = Join-Path $root 'scripts\test-startup.ps1'
    if (Test-Path -LiteralPath $startup) {
        Invoke-Step -Id 'S10' -Name '启动集成检查（中文/空格目录、旧库升级、前端启动失败）' -LogName 'startup' -Body {
            Invoke-Native -Exe 'pwsh' -WorkDir $root -What '启动集成检查' -Arguments @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startup
            )
        }
    }
}

# ================================================================ 渲染报告
Write-Head '生成报告'

$stepsPath = Join-Path $OutDir 'steps.json'
# 用 .NET 写，避免 BOM 干扰后续 JSON 解析。
[System.IO.File]::WriteAllText($stepsPath, ($steps | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))

$render = Join-Path $toolsDir 'gen_dashboard.py'
if (-not (Test-Path -LiteralPath $render)) {
    Write-Host "  [警告] 缺少 $render，跳过仪表盘生成" -ForegroundColor Magenta
} else {
    $renderArgs = @(
        $render,
        '--out-dir', $OutDir,
        '--tier', $Tier,
        '--scope', $Scope,
        '--project-root', $root,
        '--docs-dir', $docsDir,
        '--stamp', $stamp,
        '--steps-json', $stepsPath
    )
    & $python @renderArgs
    if ($LASTEXITCODE -ne 0) { Write-Host '  [警告] 仪表盘生成失败，详见上方输出' -ForegroundColor Magenta }
}

$failed = @($steps | Where-Object { $_.status -ne 'passed' })
$totalSeconds = [math]::Round((($steps | Measure-Object -Property seconds -Sum).Sum), 1)

Write-Head '结果'
Write-Host ("  步骤 {0} 个，通过 {1}，失败 {2}，总耗时 {3} 秒" -f $steps.Count, ($steps.Count - $failed.Count), $failed.Count, $totalSeconds)
foreach ($s in $steps) {
    $mark = if ($s.status -eq 'passed') { 'PASS' } else { 'FAIL' }
    $c = if ($s.status -eq 'passed') { 'Green' } else { 'Red' }
    Write-Host ("   [{0}] {1} {2,7}s  {3}" -f $s.id, $mark, $s.seconds, $s.name) -ForegroundColor $c
}

Write-Host ''
if (Test-Path -LiteralPath $dashboardPath) { Write-Host "  仪表盘    : $dashboardPath" -ForegroundColor Cyan }
if (Test-Path -LiteralPath $summaryPath) { Write-Host "  文本汇总  : $summaryPath" -ForegroundColor Cyan }
if (Test-Path -LiteralPath $resultsPath) { Write-Host "  结果数据  : $resultsPath" -ForegroundColor Cyan }
Write-Host "  原始日志  : $logDir" -ForegroundColor Cyan

if (-not $NoOpen -and (Test-Path -LiteralPath $dashboardPath)) {
    Write-Host ''
    Write-Host '  正在打开仪表盘...' -ForegroundColor Cyan
    try { Start-Process $dashboardPath } catch { Write-Host '  （无法自动打开，请手动双击上面的路径）' -ForegroundColor Magenta }
}

Write-Host ''
if ($failed.Count -gt 0) {
    Write-Host "结论：存在 $($failed.Count) 个失败步骤，请查看日志定位。" -ForegroundColor Red
    exit 1
}
Write-Host '结论：全部步骤通过。' -ForegroundColor Green
exit 0
