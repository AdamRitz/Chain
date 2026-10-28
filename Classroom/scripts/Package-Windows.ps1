param(
    [string]$BuildDirectory = "../../build/classroom-20261028",
    [string]$CompilerBin = "D:/Software/Code/Language/MinGW64/ucrt64/bin",
    [switch]$Installer
)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path $PSScriptRoot -Parent
$repoRoot = Split-Path $appRoot -Parent
$buildPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $BuildDirectory))
$resourcePath = Join-Path $appRoot 'src-tauri/resources'
New-Item -ItemType Directory -Force -Path $resourcePath | Out-Null
Push-Location $appRoot
try {
    npm.cmd ci --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    foreach ($name in @('boost.exe','chain_classroom.exe')) {
        Copy-Item -LiteralPath (Join-Path $buildPath $name) -Destination $resourcePath -Force
    }
    Copy-Item -LiteralPath (Join-Path $repoRoot 'Contracts/ClassroomChat.compiled.json') -Destination $resourcePath -Force
    $webPath = Join-Path $resourcePath 'web'
    New-Item -ItemType Directory -Force -Path $webPath | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $appRoot 'dist') | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $webPath -Recurse -Force
    }
    # Copy only recursively imported compiler-runtime DLLs, never arbitrary system files.
    $queue = [System.Collections.Generic.Queue[string]]::new()
    $queue.Enqueue((Join-Path $resourcePath 'boost.exe'))
    $queue.Enqueue((Join-Path $resourcePath 'chain_classroom.exe'))
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $objdump = Join-Path $CompilerBin 'objdump.exe'
    while ($queue.Count -gt 0) {
        $binaryPath = $queue.Dequeue()
        $dependencies = & $objdump -p $binaryPath | Select-String 'DLL Name:\s*(.+)$'
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect $binaryPath" }
        foreach ($dependency in $dependencies) {
            $dllName = $dependency.Matches[0].Groups[1].Value.Trim()
            if ($seen.Add($dllName)) {
                $candidate = Join-Path $CompilerBin $dllName
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    $destination = Join-Path $resourcePath $dllName
                    Copy-Item -LiteralPath $candidate -Destination $destination -Force
                    $queue.Enqueue($destination)
                }
            }
        }
    }
    $manifest = @{
        product = 'Chain Classroom'; version = '0.1.0'
        files = @(Get-ChildItem -LiteralPath $resourcePath -File | Where-Object { $_.Name -ne 'manifest.json' } | ForEach-Object { @{name=$_.Name;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()} })
    } | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText((Join-Path $resourcePath 'manifest.json'), $manifest, [System.Text.UTF8Encoding]::new($false))
    if ($Installer) {
        npm.cmd run tauri -- build
        if ($LASTEXITCODE -ne 0) { throw 'Tauri installer build failed' }
    }
} finally { Pop-Location }
