$ErrorActionPreference = "Stop"

$daemonScript = (Resolve-Path (Join-Path $PSScriptRoot "run-better-call-sol-daemon.ps1")).Path
$tunnelScript = (Resolve-Path (Join-Path $PSScriptRoot "run-better-call-sol-tunnel.ps1")).Path
$powerShell = (Get-Command pwsh.exe -ErrorAction Stop).Source
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$tasks = @(
    @{
        Name = "BetterCallSol-Daemon"
        Description = "Runs the local Better Call Sol Vapi-to-Codex bridge."
        Script = $daemonScript
    },
    @{
        Name = "BetterCallSol-Tunnel"
        Description = "Publishes the Better Call Sol webhook through localtunnel."
        Script = $tunnelScript
    }
)

foreach ($task in $tasks) {
    $quotedScript = '"' + $task.Script + '"'
    $action = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $quotedScript"
    Register-ScheduledTask `
        -TaskName $task.Name `
        -Description $task.Description `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
}

Write-Output "Registered BetterCallSol-Daemon and BetterCallSol-Tunnel for $userId."
