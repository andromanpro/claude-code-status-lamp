# Register the lamp daemon to run at logon (Windows, no admin required).
#
# The daemon (lamp_daemon.py) re-evaluates the shared session state on a timer
# and applies the winning preset, so a session that goes quiet is demoted to
# green / off even when no hook fires. The Claude Code hooks keep working as-is.
#
# 1. Edit $PythonW below to your pythonw.exe (runs without a console window).
# 2. Run:  powershell -ExecutionPolicy Bypass -File examples\install-daemon.ps1
#
# Manage afterwards:
#   schtasks /Run    /TN ClaudeLampDaemon     # start now
#   schtasks /End    /TN ClaudeLampDaemon     # stop
#   schtasks /Delete /TN ClaudeLampDaemon /F  # remove

$PythonW = "C:\Python314\pythonw.exe"                       # <-- your pythonw.exe
$Daemon  = (Resolve-Path "$PSScriptRoot\..\lamp_daemon.py").Path

$action  = New-ScheduledTaskAction -Execute $PythonW -Argument ('"{0}"' -f $Daemon)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME)
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # 0 = always-on, no run-time limit

Register-ScheduledTask -TaskName "ClaudeLampDaemon" -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited -Description "Background arbiter for the Claude status lamp" -Force | Out-Null
Start-ScheduledTask -TaskName "ClaudeLampDaemon"

Write-Host "ClaudeLampDaemon registered and started ($Daemon)."
