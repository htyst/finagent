function Get-ProjectNode {
    param([Parameter(Mandatory)][string]$ProjectRoot)
    $systemNode = Get-Command node -CommandType Application -ErrorAction SilentlyContinue
    if ($systemNode) { return $systemNode.Source }
    $tools = Join-Path $ProjectRoot 'var\tools'
    $candidates = @(Get-ChildItem -LiteralPath $tools -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^node-v24\.\d+\.\d+-win-x64$' } |
        Sort-Object { [version]($_.Name -replace '^node-v','' -replace '-win-x64$','') } -Descending)
    foreach ($candidate in $candidates) {
        $executable = Join-Path $candidate.FullName 'node.exe'
        if (Test-Path -LiteralPath $executable) { return $executable }
    }
    throw 'Node.js is missing. Install Node 24 or run scripts/setup-node.ps1 for a project-local portable runtime.'
}
