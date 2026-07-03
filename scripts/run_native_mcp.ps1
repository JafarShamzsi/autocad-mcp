param(
    [string]$Config = "Release",
    [switch]$NoBuild,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ServerArgs
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$exe = Join-Path $repoRoot "cpp\build\autocad-mcp-native.exe"

if ((-not $NoBuild) -and ((-not (Test-Path $exe)) -or ($env:AUTOCAD_MCP_NATIVE_REBUILD -eq "1"))) {
    $buildOutput = & (Join-Path $PSScriptRoot "build_native.ps1") -Config $Config 2>&1
    $buildExitCode = $LASTEXITCODE
    foreach ($line in $buildOutput) {
        [Console]::Error.WriteLine($line)
    }
    if ($buildExitCode -ne 0) {
        exit $buildExitCode
    }
}

if (-not (Test-Path $exe)) {
    Write-Error "Native MCP executable not found: $exe"
    exit 2
}

& $exe @ServerArgs
exit $LASTEXITCODE
