param([ValidateSet('manual','real')][string]$Clock='manual',
      [ValidateSet('live','mock')][string]$Mode='mock',
      [ValidateSet('model','legacy')][string]$Planner='model',
      [ValidateSet('on','off')][string]$Trace='on', [int]$Port=8000,
      [string]$PythonPath)
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $PythonPath) {
    $PythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
    & $PythonPath -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $installedPython = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($installedPython) { $PythonPath = $installedPython.Source }
    }
}
if (-not (Test-Path -LiteralPath $PythonPath)) { throw '找不到 Python，请通过 -PythonPath 指定可用的 Python 3.12 路径。' }
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = (Join-Path $projectRoot '.venv/Lib/site-packages') + ';' + (Join-Path $projectRoot 'backend')
Push-Location (Join-Path $projectRoot 'backend')
try {
    & $PythonPath -m app.run --clock $Clock --mode $Mode --planner $Planner --trace $Trace --port $Port
    if ($LASTEXITCODE -ne 0) { throw '启动失败，请检查 Python 3.12 与项目依赖。' }
} finally { Pop-Location; $env:PYTHONPATH = $previousPythonPath }
