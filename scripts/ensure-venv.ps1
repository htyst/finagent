function Test-SupportedPython {
    param([string]$Command, [string[]]$Arguments = @(), [switch]$RequireVenv)
    try {
        $check = if ($RequireVenv) { 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) and sys.prefix != sys.base_prefix else 1)' } else { 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)' }
        & $Command @Arguments -c $check *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Initialize-ProjectVenv {
    param([Parameter(Mandatory)][string]$ProjectRoot, [string]$Python = '', [switch]$Rebuild)
    $projectPath = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $env:PYTHONPYCACHEPREFIX = Join-Path $projectPath 'var\cache\python'
    $venvPath = [System.IO.Path]::GetFullPath((Join-Path $projectPath '.venv'))
    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    if (-not $Python -and -not $Rebuild -and (Test-SupportedPython -Command $venvPython -RequireVenv)) { return $venvPython }

    # Find a working base interpreter before touching the old environment.
    $candidates = @()
    if ($Python) {
        $candidates += @{ Command = $Python; Arguments = @() }
    } else {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            foreach ($version in @('-3.12', '-3.13', '-3.14')) {
                $candidates += @{ Command = 'py'; Arguments = @($version) }
            }
        }
        if (Get-Command python -ErrorAction SilentlyContinue) {
            $candidates += @{ Command = 'python'; Arguments = @() }
        }
    }
    $selected = $null
    foreach ($candidate in $candidates) {
        if (Test-SupportedPython -Command $candidate.Command -Arguments $candidate.Arguments) {
            $selected = $candidate
            break
        }
    }
    if ($null -eq $selected) {
        throw '项目虚拟环境缺失或不可运行。未找到可用的 Python 3.12—3.14；请用 setup.ps1 -Python 指定有效解释器。原环境未修改。'
    }

    $backupPath = $null
    if (Test-Path -LiteralPath $venvPath) {
        # Only move the exact project-local directory; never follow junctions.
        $venvItem = Get-Item -Force -LiteralPath $venvPath
        $varPath = [System.IO.Path]::GetFullPath((Join-Path $projectPath 'var'))
        if ($venvItem.Parent.FullName -ne $projectPath -or
            ($venvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw '拒绝移动项目目录之外或链接形式的 .venv。'
        }
        if ((Test-Path -LiteralPath $varPath) -and
            ((Get-Item -Force -LiteralPath $varPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw '拒绝将旧环境备份到链接形式的 var 目录。'
        }
        $backupPath = [System.IO.Path]::GetFullPath((Join-Path $varPath ('venv-backup-' + [guid]::NewGuid().ToString('N'))))
        if (-not $backupPath.StartsWith($projectPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw '虚拟环境备份路径不在项目内。'
        }
        New-Item -ItemType Directory -Force -Path $varPath | Out-Null
        Move-Item -LiteralPath $venvPath -Destination $backupPath
        Write-Host "旧虚拟环境已保留备份：$backupPath"
    }
    $command = $selected.Command
    $arguments = $selected.Arguments
    & $command @arguments -m venv $venvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-SupportedPython -Command $venvPython -RequireVenv)) {
        throw "创建项目虚拟环境失败。请检查 Python 的 venv/ensurepip；旧环境备份：$backupPath"
    }
    return $venvPython
}
