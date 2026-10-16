param([ValidateSet('mock','live')][string]$Mode='mock', [ValidateRange(1,10)][int]$Repeat=1)
$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$runRoot=Join-Path $root "var\rehearsals\$stamp-$Mode"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$oldRecord=$env:FINAGENT_RECORD_DIR
$results=@()
try {
    for ($i=1; $i -le $Repeat; $i++) {
        $env:FINAGENT_RECORD_DIR=Join-Path $runRoot "run-$i"
        New-Item -ItemType Directory -Force -Path $env:FINAGENT_RECORD_DIR | Out-Null
        try {
            & (Join-Path $PSScriptRoot 'browser-m5.ps1') -Mode $Mode
            $results += @{run=$i;passed=$true}
        } catch {
            $results += @{run=$i;passed=$false;error=$_.Exception.Message}
        }
    }
} finally {
    $env:FINAGENT_RECORD_DIR=$oldRecord
    $source=Get-FileHash (Join-Path $root 'frontend\tests\m5-smoke.mjs') -Algorithm SHA256
    @{mode=$Mode;runs=$results;script_sha256=$source.Hash;recorded_at=(Get-Date -Format o)} | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $runRoot 'summary.json')
}
if (@($results | Where-Object { -not $_.passed }).Count) { throw "Rehearsal failed; inspect $runRoot" }
Write-Host "Rehearsal passed: $Repeat runs; $runRoot"
