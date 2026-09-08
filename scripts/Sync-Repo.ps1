[CmdletBinding()]
param(
    [string]$Message = '',
    [string[]]$Paths = @(),
    [switch]$All
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    if ($All -and $Paths.Count -gt 0) { throw 'Use either -All or -Paths.' }
    $branch = & git symbolic-ref --quiet --short HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Detached HEAD: select a branch before syncing.' }
    $conflicts = @(& git diff --name-only --diff-filter=U)
    if ($LASTEXITCODE -ne 0 -or $conflicts.Count -gt 0) { throw 'Resolve merge conflicts before syncing.' }
    foreach ($state in @('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer')) {
        $statePath = & git rev-parse --git-path $state
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Git operation state.' }
        if (Test-Path -LiteralPath $statePath) { throw "Finish the active Git operation first: $state" }
    }
    if ($All -or $Paths.Count -gt 0) {
        if ([string]::IsNullOrWhiteSpace($Message)) { throw 'Provide -Message when staging changes.' }
        if ($All) { & git add --all -- . }
        else { & git add --all -- $Paths }
        if ($LASTEXITCODE -ne 0) { throw 'git add failed.' }
    }
    & git diff --cached --quiet --exit-code
    $diffExit = $LASTEXITCODE
    if ($diffExit -gt 1) { throw 'Cannot inspect staged changes.' }
    if ($diffExit -eq 1) {
        if ([string]::IsNullOrWhiteSpace($Message)) { throw 'Provide -Message to commit staged changes.' }
        & git diff --cached --stat
        $oldSkip = [Environment]::GetEnvironmentVariable('CHAIN_SKIP_AUTO_PUSH', 'Process')
        try {
            $env:CHAIN_SKIP_AUTO_PUSH = '1'
            & git commit -m $Message
            if ($LASTEXITCODE -ne 0) { throw 'git commit failed; nothing was pushed.' }
        } finally {
            [Environment]::SetEnvironmentVariable('CHAIN_SKIP_AUTO_PUSH', $oldSkip, 'Process')
        }
    } else {
        Write-Host 'No staged changes; checking pending local commits.'
    }
    $remote = & git config --get chain.syncRemote
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remote)) { $remote = 'origin' }
    & git push --set-upstream $remote "HEAD:refs/heads/$branch"
    if ($LASTEXITCODE -ne 0) { throw 'Push failed. Local commits are safe. Resolve the remote/network issue and rerun this script.' }
    $localHead = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect local HEAD.' }
    $remoteLines = @(& git ls-remote --exit-code $remote "refs/heads/$branch")
    if ($LASTEXITCODE -ne 0 -or $remoteLines.Count -ne 1) { throw 'Push completed, but remote verification failed; rerun to verify.' }
    $remoteHead = ($remoteLines[0] -split '\s+')[0]
    if ($localHead -ne $remoteHead) { throw 'Remote branch changed during verification; inspect before retrying.' }
    Write-Host "Verified local and remote HEAD: $localHead"
} finally {
    Pop-Location
}
