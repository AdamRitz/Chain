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
$configuration = Get-Content -LiteralPath (Join-Path $appRoot 'src-tauri/tauri.conf.json') -Raw | ConvertFrom-Json
$version = $configuration.version
Push-Location $appRoot
try {
    npm.cmd ci --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    # resources/ is generated staging only. Verify the exact absolute path before
    # removing stale hashed web assets and DLLs from previous builds.
    $expectedResources = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'Classroom/src-tauri/resources'))
    if ([System.IO.Path]::GetFullPath($resourcePath) -ne $expectedResources) { throw 'Unexpected resource staging path' }
    if (Test-Path -LiteralPath $resourcePath) {
        $staging = Get-Item -LiteralPath $resourcePath
        if ($staging.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Resource staging must not be a link' }
        if (@(Get-ChildItem -LiteralPath $resourcePath -Recurse -Force | Where-Object { $_.Attributes -band [System.IO.FileAttributes]::ReparsePoint }).Count) { throw 'Resource staging contains a link' }
        Remove-Item -LiteralPath $resourcePath -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resourcePath | Out-Null
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
        product = 'Chain Classroom'; version = $version
        files = @(Get-ChildItem -LiteralPath $resourcePath -File -Recurse | Sort-Object FullName | ForEach-Object { @{name=$_.FullName.Substring($resourcePath.Length + 1).Replace('\','/');sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()} })
    } | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText((Join-Path $resourcePath 'manifest.json'), $manifest, [System.Text.UTF8Encoding]::new($false))
    if ($Installer) {
        npm.cmd run tauri -- build
        if ($LASTEXITCODE -ne 0) { throw 'Tauri installer build failed' }
        $installerPath = Join-Path $appRoot "src-tauri/target/release/bundle/nsis/Chain Classroom_${version}_x64-setup.exe"
        $releasePath = Join-Path $appRoot 'release'
        New-Item -ItemType Directory -Force -Path $releasePath | Out-Null
        $releaseInstaller = Join-Path $releasePath "ChainClassroom-${version}-windows-x64-setup.exe"
        Copy-Item -LiteralPath $installerPath -Destination $releaseInstaller -Force
        $hash = (Get-FileHash -LiteralPath $releaseInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
        [System.IO.File]::WriteAllText("$releaseInstaller.sha256", "$hash  $([System.IO.Path]::GetFileName($releaseInstaller))`n", [System.Text.UTF8Encoding]::new($false))
        Write-Output "One-click installer: $releaseInstaller"
        Write-Output "SHA256: $hash"
    }
} finally { Pop-Location }
