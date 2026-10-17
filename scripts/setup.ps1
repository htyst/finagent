param([string]$Python = "", [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'ensure-venv.ps1')
$venvPython = Initialize-ProjectVenv -ProjectRoot $root -Python $Python -Rebuild:$Rebuild

$lock = Join-Path $root 'backend\requirements.lock'
if (Test-Path -LiteralPath $lock) {
    & $venvPython -m pip install -r $lock
    if ($LASTEXITCODE -ne 0) { throw '锁定依赖安装失败' }
    & $venvPython -m pip install --no-deps -e (Join-Path $root 'backend')
} else {
    & $venvPython -m pip install -e (Join-Path $root 'backend[dev]')
}
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
. (Join-Path $PSScriptRoot 'resolve-node.ps1')
$node = Get-ProjectNode -ProjectRoot $root
$npmCli = Join-Path (Split-Path $node) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $npmCli)) { throw 'Node安装中缺少npm，请修复Node环境。' }
$savedNodePath = $env:PATH
$env:PATH = (Split-Path $node) + ';' + $env:PATH
Push-Location (Join-Path $root 'frontend')
try {
    if (Test-Path -LiteralPath 'package-lock.json') { & $node $npmCli ci }
    else { & $node $npmCli install }
    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
} finally { Pop-Location; $env:PATH = $savedNodePath }
Write-Host '依赖安装完成。首次使用年度案例请运行 .\scripts\import-financial-demo.ps1；已有年度数据库时直接启动，无需重新导入。'
