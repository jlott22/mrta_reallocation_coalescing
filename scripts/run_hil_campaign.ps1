[CmdletBinding()]
param(
    [string]$Config = "configs/hil_campaign.json",
    [string[]]$Ports = @("auto"),
    [switch]$DryRun,
    [switch]$RunPreflight
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigPath = if ([System.IO.Path]::IsPathRooted($Config)) {
    (Resolve-Path $Config).Path
} else {
    (Resolve-Path (Join-Path $RepoRoot $Config)).Path
}
$env:PYTHONPATH = (Resolve-Path (Join-Path $RepoRoot "Simulation/Architecture")).Path

Push-Location $RepoRoot
try {
    python -m allocator_replay hil-validate --config $ConfigPath
    if ($LASTEXITCODE -ne 0) { throw "HIL manifest validation failed" }

    if ($DryRun) {
        python -m allocator_replay hil-dry-run --config $ConfigPath --devices 1
        if ($LASTEXITCODE -ne 0) { throw "HIL software loopback failed" }
        return
    }

    python -m allocator_replay discover --ports $Ports
    if ($LASTEXITCODE -ne 0) { throw "HIL device discovery failed" }
    if ($RunPreflight) {
        python -m allocator_replay preflight --ports $Ports
        if ($LASTEXITCODE -ne 0) { throw "HIL collaborative preflight failed" }
    }
    python -m allocator_replay hil-run --config $ConfigPath --ports $Ports
    if ($LASTEXITCODE -ne 0) { throw "HIL campaign failed or paused; rerun to resume" }
    python -m allocator_replay hil-report --config $ConfigPath
    if ($LASTEXITCODE -ne 0) { throw "HIL report rebuild failed" }
}
finally {
    Pop-Location
}
