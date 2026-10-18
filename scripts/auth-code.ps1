param([string]$ChallengeId = '')
$projectRoot = Split-Path -Parent $PSScriptRoot
$outbox = Join-Path $projectRoot 'var\auth-simulator'
if ($ChallengeId -and $ChallengeId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid challenge ID' }
$file = if ($ChallengeId) { Get-Item -LiteralPath (Join-Path $outbox ($ChallengeId + '.json')) } else {
    Get-ChildItem -LiteralPath $outbox -Filter '*.json' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if (-not $file) { throw 'No simulated SMS. Request a challenge from the page first.' }
$sms = Get-Content -LiteralPath $file.FullName -Encoding UTF8 | ConvertFrom-Json
Write-Host 'LOCAL SMS SIMULATOR - not real SMS or bank identity verification'
Write-Host "Challenge: $($sms.challenge_id)"
Write-Host "Expires: $($sms.expires_at)"
Write-Host "Code: $($sms.code)"
