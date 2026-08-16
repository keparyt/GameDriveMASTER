$ErrorActionPreference = "Stop"

$gameDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vacuumTube = Join-Path $gameDir "VacuumTube.exe"

if (-not (Test-Path -LiteralPath $vacuumTube -PathType Leaf)) {
    throw "VacuumTube.exe was not found at: $vacuumTube"
}

# VacuumTube officially supports --fullscreen, so use its native fullscreen mode.
$process = Start-Process -FilePath $vacuumTube -ArgumentList @("--fullscreen") -WorkingDirectory $gameDir -PassThru

# Wait briefly for Electron/VacuumTube to create its native window, then force focus to it.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WindowFocus {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    try {
        $process.Refresh()
        if ($process.HasExited) {
            exit $process.ExitCode
        }

        if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
            # SW_SHOWNORMAL keeps the native fullscreen state requested by VacuumTube.
            [WindowFocus]::ShowWindow($process.MainWindowHandle, 1) | Out-Null
            [WindowFocus]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
            break
        }
    } catch {
        # Electron can take a moment to expose its window handle.
    }
    Start-Sleep -Milliseconds 150
}

# A second focus attempt handles the common Electron startup race.
Start-Sleep -Milliseconds 250
try {
    $process.Refresh()
    if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
        [WindowFocus]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
    }
} catch {
}
