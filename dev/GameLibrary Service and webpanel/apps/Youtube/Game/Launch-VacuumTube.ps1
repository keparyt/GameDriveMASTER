$ErrorActionPreference = "Stop"

$gameDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vacuumTube = Join-Path $gameDir "VacuumTube.exe"

if (-not (Test-Path -LiteralPath $vacuumTube -PathType Leaf)) {
    throw "VacuumTube.exe was not found in $gameDir"
}

# Chromium/Electron fullscreen mode.
$process = Start-Process -FilePath $vacuumTube `
    -ArgumentList @("--start-fullscreen") `
    -WorkingDirectory $gameDir `
    -PassThru

# Give Electron a moment to create its window, then force it to the foreground.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WindowFocus {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    try {
        $process.Refresh()
        if ($process.HasExited) { break }
        if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
            [WindowFocus]::ShowWindow($process.MainWindowHandle, 3) | Out-Null
            [WindowFocus]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
            break
        }
    } catch {
        # The process may still be initializing its Electron window.
    }
    Start-Sleep -Milliseconds 200
}
