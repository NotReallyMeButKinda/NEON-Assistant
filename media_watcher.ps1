# media_watcher.ps1 -- long-running helper started by media.py.
#
# Reads Windows' media sessions (GlobalSystemMediaTransportControlsSessionManager: what Spotify, a
# browser tab, Pear Desktop... report to the volume flyout) and prints one JSON object per line:
#   {"type":"media","app":"Spotify.exe","title":"...","artist":"...","status":"Playing",
#    "position":12.3,"duration":201.0}                     the session that matters, every poll
#   {"type":"media","app":""}                             nothing is playing or paused anywhere
#   {"type":"error","message":"..."}
# Commands arrive through the file given as -CmdFile, one per line: play | pause | toggle | next | previous.
# They go to the same session that is being reported. If -ParentPid disappears, this helper exits too.
param([Parameter(Mandatory = $true)][string]$CmdFile, [int]$PollMs = 1000, [int]$ParentPid = 0)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]

function Emit($obj) { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 5)); [Console]::Out.Flush() }

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    if (-not $task.Wait(5000)) { throw "the media service didn't answer" }
    $task.Result
}

$ns = "Windows.Media.Control.GlobalSystemMediaTransportControlsSession"
$propsType = [Type]"$($ns)MediaProperties"

function Pick($mgr) {
    # A session that is playing wins; otherwise the one Windows calls current (usually the last used).
    foreach ($s in $mgr.GetSessions()) {
        if ("$($s.GetPlaybackInfo().PlaybackStatus)" -eq "Playing") { return $s }
    }
    return $mgr.GetCurrentSession()
}

try {
    $mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    while ($true) {
        if ($ParentPid -gt 0 -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { return }
        try {
            $s = Pick $mgr
            if (Test-Path -LiteralPath $CmdFile) {
                $wanted = @(Get-Content -LiteralPath $CmdFile -ErrorAction SilentlyContinue)
                Remove-Item -LiteralPath $CmdFile -ErrorAction SilentlyContinue
                if ($s) {
                    foreach ($cmd in $wanted) {
                        switch ($cmd.Trim()) {
                            "play"     { $null = Await ($s.TryPlayAsync()) ([bool]) }
                            "pause"    { $null = Await ($s.TryPauseAsync()) ([bool]) }
                            "toggle"   { $null = Await ($s.TryTogglePlayPauseAsync()) ([bool]) }
                            "next"     { $null = Await ($s.TrySkipNextAsync()) ([bool]) }
                            "previous" { $null = Await ($s.TrySkipPreviousAsync()) ([bool]) }
                        }
                    }
                    Start-Sleep -Milliseconds 150       # let the player report its new state
                    $s = Pick $mgr
                }
            }
            if (-not $s) {
                Emit @{ type = "media"; app = "" }
            } else {
                $props = Await ($s.TryGetMediaPropertiesAsync()) $propsType
                $timeline = $s.GetTimelineProperties()
                $status = "$($s.GetPlaybackInfo().PlaybackStatus)"
                $position = $timeline.Position.TotalSeconds
                $duration = ($timeline.EndTime - $timeline.StartTime).TotalSeconds
                if ($status -eq "Playing" -and $timeline.LastUpdatedTime.Year -gt 2000) {
                    $position += ([DateTimeOffset]::Now - $timeline.LastUpdatedTime).TotalSeconds
                }
                if ($duration -gt 0 -and $position -gt $duration) { $position = $duration }
                Emit @{ type = "media"; app = [string]$s.SourceAppUserModelId; title = [string]$props.Title
                        artist = [string]$props.Artist; status = $status; position = [math]::Round($position, 2)
                        duration = [math]::Round($duration, 2) }
            }
        } catch {
            Emit @{ type = "error"; message = $_.Exception.Message }
        }
        Start-Sleep -Milliseconds $PollMs
    }
} catch {
    Emit @{ type = "error"; message = $_.Exception.Message }
}
