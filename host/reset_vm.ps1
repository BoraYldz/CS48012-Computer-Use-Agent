# Restores the VM to the "clean" snapshot and waits until the tool server answers.
# Usage:  powershell -ExecutionPolicy Bypass -File host\reset_vm.ps1
param(
    [string]$Vm = "cua",
    [string]$Snapshot = "clean",
    [string]$Health = "http://localhost:8765/health",
    [int]$TimeoutSec = 120
)
$ErrorActionPreference = "Stop"
$vbox = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
$sw = [Diagnostics.Stopwatch]::StartNew()

$running = & $vbox list runningvms | Select-String "`"$Vm`""
if ($running) { & $vbox controlvm $Vm poweroff | Out-Null; Start-Sleep -Seconds 2 }
& $vbox snapshot $Vm restore $Snapshot | Out-Null
& $vbox startvm $Vm --type headless | Out-Null

while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    try {
        $r = Invoke-RestMethod -Uri $Health -TimeoutSec 3
        if ($r.ok) { Write-Host ("VM ready in {0:N1}s" -f $sw.Elapsed.TotalSeconds); exit 0 }
    } catch { Start-Sleep -Seconds 1 }
}
Write-Error "VM did not become healthy within $TimeoutSec s"
exit 1
