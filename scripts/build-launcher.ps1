$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $csc)) { throw 'Windows .NET Framework C# compiler not found.' }
& $csc /nologo /target:winexe /optimize+ /platform:anycpu /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Net.Http.dll ('/out:' + (Join-Path $root 'FinAgent.exe')) (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed.' }
foreach ($mode in @('Live','Mock')) { Copy-Item -LiteralPath (Join-Path $root 'FinAgent.exe') -Destination (Join-Path $root "FinAgent-$mode.exe") -Force }
Write-Host 'Built FinAgent.exe, FinAgent-Live.exe and FinAgent-Mock.exe'
