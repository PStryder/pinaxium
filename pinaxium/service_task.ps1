# Pinaxium service: Windows Task Scheduler entry that runs it at logon (called by `python -m pinaxium.service`).
# ASCII only: Windows PowerShell 5.1 reads .ps1 files as ANSI.
param(
    [Parameter(Mandatory = $true)][ValidateSet("install", "uninstall", "status", "start", "stop")][string]$Action,
    [string]$TaskName = "Pinaxium",
    [string]$Python = "",
    [string]$WorkDir = ""
)
$ErrorActionPreference = "Stop"

switch ($Action) {
    "install" {
        $act = New-ScheduledTaskAction -Execute $Python -Argument "-m pinaxium.service run" -WorkingDirectory $WorkDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "Pinaxium MCP server (local image generation)" -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        "installed"
    }
    "uninstall" {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $t) { "not installed"; break }
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "uninstalled"
    }
    "status" {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $t) { "not installed"; break }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        "task {0}; last run {1}; last result {2}" -f $t.State, $info.LastRunTime, $info.LastTaskResult
    }
    "start" { Start-ScheduledTask -TaskName $TaskName; "started" }
    "stop" { Stop-ScheduledTask -TaskName $TaskName; "stopped" }
}
