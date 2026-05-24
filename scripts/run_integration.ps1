# One-shot integration test runner against live AutoCAD.
#
# Usage:
#   ./scripts/run_integration.ps1
#   ./scripts/run_integration.ps1 -OnlyCOM      # COM-suite only
#   ./scripts/run_integration.ps1 -Verbose:$true
#
# Requirements:
#   - Full AutoCAD running (any open document is fine; the suite creates
#     and tears down its own drawings).
#   - .venv populated (uv sync or pip install -e ".[dev]").
#
# Note: -Verbose is the standard PowerShell common parameter; we just
# forward it to pytest as `-v`. No explicit declaration needed.

[CmdletBinding()]
param(
    [switch]$OnlyCOM
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

# Resolve Python: project venv first, fall back to system.
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Warning "Project venv not found at $Python"
    Write-Warning 'Run `uv sync` or `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"` first.'
    exit 1
}

# Confirm AutoCAD is running before we start (saves a confusing skip cascade).
Write-Host "Probing for full AutoCAD..." -ForegroundColor Cyan
$probe = & $Python -c "from autocad_mcp.config import _running_autocad_full_version; v = _running_autocad_full_version(); print(v if v else 'NONE')"
if ($probe -eq "NONE") {
    Write-Warning "Full AutoCAD not detected in the registry — the COM suite will skip every test."
    Write-Warning "Start AutoCAD (full, not LT) and re-run."
}
else {
    Write-Host "  Detected full AutoCAD $probe" -ForegroundColor Green
}

# Build pytest invocation.
$pytestArgs = @("-m", "pytest", "-m", "integration")
if ($OnlyCOM) {
    $pytestArgs += "tests/test_com_integration.py"
}
else {
    $pytestArgs += "tests/"
}
if ($VerbosePreference -ne 'SilentlyContinue') {
    $pytestArgs += "-v"
}
$pytestArgs += "--tb=short"

Write-Host "Running: $Python $($pytestArgs -join ' ')" -ForegroundColor Cyan
& $Python @pytestArgs
exit $LASTEXITCODE
