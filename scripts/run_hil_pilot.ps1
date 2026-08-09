[CmdletBinding()]
param(
    [string]$Config = "configs/hil_pilot.json",
    [string[]]$Ports = @("auto"),
    [switch]$DryRun,
    [switch]$PrepareDevices
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

    if ($PrepareDevices) {
        python -m allocator_replay build-device --ports $Ports
        if ($LASTEXITCODE -ne 0) { throw "HIL device build failed" }
        python -m allocator_replay deploy --ports $Ports
        if ($LASTEXITCODE -ne 0) { throw "HIL deployment failed" }
    }

    python -m allocator_replay discover --ports $Ports
    if ($LASTEXITCODE -ne 0) { throw "HIL device discovery failed" }
    python -m allocator_replay preflight --ports $Ports
    if ($LASTEXITCODE -ne 0) { throw "HIL collaborative preflight failed" }
    python -m allocator_replay hil-run --config $ConfigPath --ports $Ports
    if ($LASTEXITCODE -ne 0) { throw "HIL pilot failed or paused" }
}
finally {
    Pop-Location
}
