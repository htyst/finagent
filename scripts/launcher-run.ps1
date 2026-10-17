param([ValidateSet('mock','live')][string]$Mode = 'mock')
$ErrorActionPreference = 'Stop'
try {
    if ($env:FINAGENT_START_EVENT) {
        $gate = [System.Threading.EventWaitHandle]::OpenExisting($env:FINAGENT_START_EVENT)
        try { if (-not $gate.WaitOne(15000)) { throw '启动器未完成进程管理绑定，服务未启动。' } }
        finally { $gate.Dispose() }
    }
    & (Join-Path $PSScriptRoot 'dev.ps1') -Mode $Mode
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
