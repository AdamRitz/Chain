param(
    [ValidateSet('status','configure','remove')][string]$Action = 'status',
    [ValidateSet('Any','Private','Domain','Public')][string]$Profile = 'Any',
    [string]$Remote = 'Any'
)
$ErrorActionPreference = 'Stop'
# Installed alongside the fixed-purpose helper and boost.exe. No arbitrary
# executable, command, output path, or elevated script is accepted from callers.
$helper = Join-Path $PSScriptRoot 'norn-network.exe'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    throw 'Run the copy in the installed application runtime directory.'
}
& $helper $Action $Profile $Remote
exit $LASTEXITCODE
