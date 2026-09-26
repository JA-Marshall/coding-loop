param(
    [Parameter(Mandatory=$true)][string]$Distro,
    [Parameter(Mandatory=$true)][string]$Unit,
    [Parameter(Mandatory=$true)][string]$StatusPath,
    [Parameter(Mandatory=$true)][int]$MaxSeconds
)
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class StorehouseAwake {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
"@
function Write-Status([string]$State) {
    @{state=$State; pid=$PID; unit=$Unit; updated=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $StatusPath -Encoding utf8
}
try {
    if ([StorehouseAwake]::SetThreadExecutionState([uint32]2147483649) -eq 0) {
        throw 'Windows rejected the temporary awake request'
    }
    Write-Status 'AWAKE'
    $deadline = [DateTime]::UtcNow.AddSeconds($MaxSeconds)
    $startDeadline = [DateTime]::UtcNow.AddSeconds(90)
    $observed = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $state = (& wsl.exe -d $Distro -- systemctl --user is-active $Unit 2>$null | Out-String).Trim()
        if ($state -eq 'active' -or $state -eq 'activating') {
            $observed = $true
        } elseif ($observed -or [DateTime]::UtcNow -gt $startDeadline) {
            break
        }
        Start-Sleep -Seconds 20
    }
} catch {
    Write-Status 'ERROR'
    exit 1
} finally {
    [void][StorehouseAwake]::SetThreadExecutionState([uint32]2147483648)
    Write-Status 'RELEASED'
}
