param(
    [string]$Config = "Release",
    [string]$BuildDir = "cpp\build"
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$buildPath = Join-Path $repoRoot $BuildDir

$vcvarsCandidates = @(
    "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\17\Community\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\17\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
)

$vcvars = $vcvarsCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $vcvars) {
    throw "Could not find vcvars64.bat. Install Visual Studio Build Tools with the Windows SDK."
}

$commands = @(
    "call `"$vcvars`"",
    "cmake -S `"$repoRoot\cpp`" -B `"$buildPath`" -G `"NMake Makefiles`" -DCMAKE_BUILD_TYPE=$Config",
    "cmake --build `"$buildPath`"",
    "ctest --test-dir `"$buildPath`" --output-on-failure"
) -join " && "

cmd.exe /d /c $commands
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
