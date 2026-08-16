$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$web = Join-Path $root "web"

$app = Join-Path $web "app.js"
$index = Join-Path $web "index.html"

if (!(Test-Path $app)) {
    throw "app.js not found: $app"
}

if (!(Test-Path $index)) {
    throw "index.html not found: $index"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $root "ui-backup-$timestamp"

New-Item -ItemType Directory -Path $backup | Out-Null

Copy-Item $app (Join-Path $backup "app.js")
Copy-Item $index (Join-Path $backup "index.html")

Write-Host "Backup created: $backup" -ForegroundColor Green

$appText = Get-Content $app -Raw
$indexText = Get-Content $index -Raw

# ============================================================
# 1. Fix the broken encoding character
# ============================================================

$appText = $appText.Replace("Â·", "·")
$appText = $appText.Replace("Â", "")

# ============================================================
# 2. Add a safe byte formatter
# ============================================================

$safeFormatter = @'
function formatDriveCapacity(value) {
    const bytes = Number(value);

    if (!Number.isFinite(bytes) || bytes <= 0) {
        return "Unknown size";
    }

    const units = ["B", "KB", "MB", "GB", "TB"];

    let size = bytes;
    let unit = 0;

    while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit++;
    }

    const decimals = unit >= 3 ? 1 : 0;

    return `${size.toFixed(decimals)} ${units[unit]}`;
}

'@

if ($appText -notmatch "function formatDriveCapacity") {

    # Insert before the hardware card renderer.
    $marker = "function createHardwareCard"

    if ($appText.Contains($marker)) {
        $appText = $appText.Replace(
            $marker,
            $safeFormatter + $marker
        )

        Write-Host "Added safe drive capacity formatter." -ForegroundColor Green
    }
    else {
        Write-Host "WARNING: createHardwareCard not found." -ForegroundColor Yellow
    }
}

# ============================================================
# 3. Replace hardware card renderer
# ============================================================

$hardwarePattern = '(?s)function createHardwareCard\(d\)\{.*?\}\s*function patchDrives'

$hardwareReplacement = @'
function createHardwareCard(d) {
    const connected =
        !d.offline &&
        String(d.status || "").toLowerCase() === "online";

    const bus = String(d.bus || "Unknown").trim();

    const capacity = formatDriveCapacity(d.size);

    const parts = Array.isArray(d.partitions)
        ? d.partitions
        : [];

    return `
        <div class="hardware-card ${connected ? "connected" : "disconnected"}">

            <div class="hardware-head">

                <span class="hardware-icon">
                    ${escapeHtml(bus)}
                </span>

                <div class="hardware-main">

                    <strong>
                        ${escapeHtml(d.name || "Unknown drive")}
                    </strong>

                    <small>
                        ${escapeHtml(bus)}
                        <span class="drive-separator">·</span>
                        ${escapeHtml(capacity)}
                    </small>

                </div>

                <b>
                    ${connected ? "CONNECTED" : "OFFLINE"}
                </b>

            </div>

            ${
                parts.length
                    ? `
                        <div class="partition-list">
                            ${parts.map(createPartitionRow).join("")}
                        </div>
                      `
                    : ""
            }

        </div>
    `;
}

function patchDrives
'@

if ($appText -match $hardwarePattern) {

    $appText = [regex]::Replace(
        $appText,
        $hardwarePattern,
        $hardwareReplacement,
        1
    )

    Write-Host "Updated drive cards." -ForegroundColor Green
}
else {
    Write-Host "WARNING: createHardwareCard pattern not found." -ForegroundColor Yellow
}

# ============================================================
# 4. Fix partition display
# ============================================================

$partitionPattern = '(?s)function createPartitionRow\(p\)\{.*?\}\s*function formatDriveCapacity'

$partitionReplacement = @'
function createPartitionRow(p) {

    const letter = String(p.letter || "")
        .replace(/\0/g, "")
        .trim();

    if (!letter) {
        return "";
    }

    const loaded = Boolean(
        p.gamedrive_loaded ||
        p.game_drive ||
        p.gamedrive
    );

    return `
        <div class="partition-row ${loaded ? "loaded" : ""}">
            <span class="partition-dot"></span>
            <strong>${escapeHtml(letter)}:</strong>
            <em>${loaded ? "GameDrive" : "Available"}</em>
        </div>
    `;
}

function formatDriveCapacity
'@

if ($appText -match $partitionPattern) {

    $appText = [regex]::Replace(
        $appText,
        $partitionPattern,
        $partitionReplacement,
        1
    )

    Write-Host "Updated partition display." -ForegroundColor Green
}

# ============================================================
# 5. Drives-only visibility
# ============================================================

$visibilityFunction = @'
function updateDevicesPanelVisibility() {

    const panel =
        document.querySelector("#devices-panel") ||
        document.querySelector("#drives-box");

    if (!panel) {
        return;
    }

    // Only show the hardware/device list on the Drives page.
    const drivesView =
        currentView === "drives" ||
        currentView === "drive";

    panel.hidden = !drivesView;
}

'@

# Remove an old version if it exists.
$appText = [regex]::Replace(
    $appText,
    '(?s)function updateDevicesPanelVisibility\(\)\s*\{.*?\n\}',
    "",
    1
)

$insertMarker = "function createPartitionRow"

if ($appText.Contains($insertMarker)) {

    $appText = $appText.Replace(
        $insertMarker,
        $visibilityFunction + $insertMarker
    )

    Write-Host "Added Drives-only visibility." -ForegroundColor Green
}

# ============================================================
# 6. Make visibility react to tab changes
# ============================================================

# Find currentView assignments and append visibility update.
$pattern = 'currentView\s*=\s*([^;]+);'

$matches = [regex]::Matches($appText, $pattern)

if ($matches.Count -gt 0) {

    # Work backwards so indexes don't change.
    for ($i = $matches.Count - 1; $i -ge 0; $i--) {

        $m = $matches[$i]

        $afterIndex = $m.Index + $m.Length

        $remaining = $appText.Substring(
            $afterIndex,
            [Math]::Min(
                120,
                $appText.Length - $afterIndex
            )
        )

        if ($remaining -notmatch "updateDevicesPanelVisibility") {

            $appText =
                $appText.Substring(0, $afterIndex) +
                "`nupdateDevicesPanelVisibility();" +
                $appText.Substring($afterIndex)
        }
    }

    Write-Host "Connected device panel to tab switching." -ForegroundColor Green
}

# ============================================================
# 7. CSS cleanup
# ============================================================

$css = @'
<style id="compact-device-panel-v2">

.hardware-card {
    padding: 9px 11px !important;
    margin-bottom: 6px !important;
    border-radius: 9px !important;
}

.hardware-head {
    min-height: 38px !important;
    gap: 9px !important;
}

.hardware-icon {
    font-size: 10px !important;
    min-width: 32px;
}

.hardware-main {
    min-width: 0;
    flex: 1;
}

.hardware-head strong {
    display: block;
    font-size: 13px !important;
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.hardware-head small {
    display: block;
    font-size: 10px !important;
    margin-top: 2px;
}

.drive-separator {
    opacity: .55;
    margin: 0 3px;
}

.hardware-head b {
    font-size: 9px !important;
    white-space: nowrap;
}

.partition-list {
    margin-top: 5px !important;
    padding-top: 5px !important;
}

.partition-row {
    min-height: 18px !important;
    padding: 2px 0 !important;
    font-size: 10px !important;
}

.partition-row em {
    font-size: 9px !important;
}

</style>
'@

if ($indexText -notmatch "compact-device-panel-v2") {

    $indexText = $indexText.Replace(
        "</head>",
        $css + "`n</head>"
    )

    Write-Host "Added compact device CSS." -ForegroundColor Green
}

# ============================================================
# 8. Force device panel hidden initially
# ============================================================

if ($indexText -match 'id="devices-panel"') {

    $indexText = [regex]::Replace(
        $indexText,
        '<([^>]*id="devices-panel"[^>]*)>',
        {
            param($m)

            $tag = $m.Groups[1].Value

            if ($tag -notmatch '\bhidden\b') {
                $tag += ' hidden'
            }

            return "<$tag>"
        },
        1
    )

    Write-Host "Device panel starts hidden." -ForegroundColor Green
}

# ============================================================
# Save
# ============================================================

Set-Content -Path $app -Value $appText -Encoding UTF8
Set-Content -Path $index -Value $indexText -Encoding UTF8

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "Device UI update completed." -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Backup:"
Write-Host $backup
Write-Host ""
Write-Host "IMPORTANT:"
Write-Host "Hard refresh the web UI with Ctrl + Shift + R"
Write-Host ""
