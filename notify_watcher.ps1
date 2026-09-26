# notify_watcher.ps1 -- long-running helper started by notifications.py.
#
# Polls Windows' UserNotificationListener (the official notification-reading API; it works for
# ordinary desktop apps) and prints one JSON object per line on stdout:
#   {"type":"access","status":"Allowed"}          the permission state, once at start
#   {"type":"toast",...,"backlog":true}          (first poll only) a notification that was already waiting when the helper started
#   {"type":"ready"}                              the backlog is complete; from now on only NEW ones are printed
#   {"type":"toast","id":123,"app":"Discord","aumid":"com.squirrel.Discord.Discord","texts":["Title","Body",...],"created":1700000000000}
#   {"type":"error","message":"..."}
# Commands arrive through the file given as -CmdFile: one notification id per line to dismiss.
# If the process named by -ParentPid disappears (the app was killed or crashed), this helper exits too.
param([Parameter(Mandatory = $true)][string]$CmdFile, [int]$PollMs = 800, [int]$ParentPid = 0)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.UI.Notifications.UserNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]

function Emit($obj) { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 5)); [Console]::Out.Flush() }

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    if (-not $task.Wait(10000)) { throw "the notification service didn't answer" }
    $task.Result
}

try {
    $listener = [Windows.UI.Notifications.Management.UserNotificationListener]::Current
    $status = Await ($listener.RequestAccessAsync()) ([Windows.UI.Notifications.Management.UserNotificationListenerAccessStatus])
    Emit @{ type = "access"; status = "$status" }
    if ("$status" -ne "Allowed") { return }

    $listType = [System.Collections.Generic.IReadOnlyList[Windows.UI.Notifications.UserNotification]]
    $seen = New-Object 'System.Collections.Generic.HashSet[uint32]'
    $first = $true
    while ($true) {
        if ($ParentPid -gt 0 -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { return }
        try {
            $list = Await ($listener.GetNotificationsAsync([Windows.UI.Notifications.NotificationKinds]::Toast)) $listType
            $ids = New-Object 'System.Collections.Generic.List[uint32]'
            foreach ($n in $list) {
                $ids.Add($n.Id)
                if ($seen.Add($n.Id)) {
                    $texts = @()
                    foreach ($binding in $n.Notification.Visual.Bindings) {
                        foreach ($el in $binding.GetTextElements()) { $texts += [string]$el.Text }
                    }
                    $aumid = ""
                    try { $aumid = [string]$n.AppInfo.AppUserModelId } catch { }
                    Emit @{ type = "toast"; id = [int]$n.Id; app = [string]$n.AppInfo.DisplayInfo.DisplayName; aumid = $aumid
                            texts = @($texts); created = $n.CreationTime.ToUnixTimeMilliseconds(); backlog = [bool]$first }
                }
            }
            $seen.IntersectWith($ids)                 # forget ids that are gone, so the set can't grow forever
            if ($first) { Emit @{ type = "ready" }; $first = $false }

            if (Test-Path -LiteralPath $CmdFile) {    # dismiss requests from the app
                $wanted = @(Get-Content -LiteralPath $CmdFile -ErrorAction SilentlyContinue)
                Remove-Item -LiteralPath $CmdFile -ErrorAction SilentlyContinue
                foreach ($line in $wanted) { if ($line -match '^\d+$') { $listener.RemoveNotification([uint32]$line) } }
            }
        } catch {
            Emit @{ type = "error"; message = $_.Exception.Message }
        }
        Start-Sleep -Milliseconds $PollMs
    }
} catch {
    Emit @{ type = "error"; message = $_.Exception.Message }
}
