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

Write-Host ""
Write-Host "Backup created:" -ForegroundColor Green
Write-Host $backup
Write-Host ""

# ------------------------------------------------------------
# app.js
# ------------------------------------------------------------

$appText = Get-Content $app -Raw

# Replace the detailed partition renderer.
$partitionPattern = '(?s)function createPartitionRow\(p\)\{.*?\}\s*function createHardwareCard'

$partitionReplacement = @'
function createPartitionRow(p) {
    const loaded = Boolean(p.gamedrive_loaded);

    const letter = String(p.letter || "")
        .replace(/\0/g, "")
        .trim();

    if (!letter) return "";

    return `
        <div class="partition-row ${loaded ? "loaded" : ""}">
            <span class="partition-dot"></span>
            <strong>${escapeHtml(letter)}:</strong>
            <em>${loaded ? "GameDrive" : "Available"}</em>
        </div>
    `;
}

function createHardwareCard
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
else {
    Write-Host "WARNING: createPartitionRow() pattern not found." -ForegroundColor Yellow
}

# Replace the hardware card renderer.
$hardwarePattern = '(?s)function createHardwareCard\(d\)\{.*?\}\s*function patchDrives'

$hardwareReplacement = @'
function createHardwareCard(d) {
    const connected =
        !d.offline &&
        String(d.status || "").toLowerCase().includes("online");

    const usb =
        String(d.bus || "").toLowerCase() === "usb";

    const parts = Array.isArray(d.partitions)
        ? d.partitions
        : [];

    return `
        <div class="hardware-card ${connected ? "connected" : "disconnected"}">
            <div class="hardware-head">
                <span class="hardware-icon">
                    ${usb ? "USB" : "▣"}
                </span>

                <div class="hardware-main">
                    <strong>${escapeHtml(d.name || "Unknown disk")}</strong>

                    <small>
                        ${escapeHtml(d.bus || "Unknown")}
                        ·
                        ${escapeHtml(formatBytes(d.size))}
                    </small>
                </div>

                <b>
                    ${connected ? "CONNECTED" : "OFFLINE"}
                </b>
            </div>

            ${
                parts.length
                    ? `<div class="partition-list">
                        ${parts.map(createPartitionRow).join("")}
                       </div>`
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

    Write-Host "Updated drive card display." -ForegroundColor Green
}
else {
    Write-Host "WARNING: createHardwareCard() pattern not found." -ForegroundColor Yellow
}

# ------------------------------------------------------------
# Device panel visibility
# ------------------------------------------------------------

$visibilityCode = @'

function updateDevicesPanelVisibility() {
    const panel =
        document.querySelector("#devices-panel") ||
        document.querySelector("#drives-box") ||
        document.querySelector("#drives");

    if (!panel) return;

    panel.hidden = currentView !== "drives";
}

'@

if ($appText -notmatch 'function updateDevicesPanelVisibility') {
    $marker = 'function gameQuery'

    if ($appText.Contains($marker)) {
        $appText = $appText.Replace(
            $marker,
            $visibilityCode + $marker
        )

        Write-Host "Added device-panel visibility function." -ForegroundColor Green
    }
    else {
        Write-Host "WARNING: Could not find insertion point for visibility function." -ForegroundColor Yellow
    }
}

# Make sure visibility is refreshed whenever currentView changes.
$currentViewPattern = 'currentView\s*=\s*([^;]+);'

$matches = [regex]::Matches($appText, $currentViewPattern)

if ($matches.Count -gt 0) {
    Write-Host "Found $($matches.Count) currentView assignment(s)." -ForegroundColor Green

    $last = $matches[$matches.Count - 1]

    $after = $last.Index + $last.Length

    if ($appText.Substring(
        $after,
        [Math]::Min(100, $appText.Length - $after)
    ) -notmatch 'updateDevicesPanelVisibility') {

        $appText =
            $appText.Substring(0, $after) +
            "`nupdateDevicesPanelVisibility();" +
            $appText.Substring($after)

        Write-Host "Added visibility refresh." -ForegroundColor Green
    }
}

Set-Content -Path $app -Value $appText -Encoding UTF8

# ------------------------------------------------------------
# index.html
# ------------------------------------------------------------

$indexText = Get-Content $index -Raw

# Add compact device CSS if it doesn't already exist.
$compactCss = @'

<style id="compact-device-panel">
/* Compact storage/device panel */
.hardware-card {
    padding: 10px 12px !important;
    border-radius: 10px !important;
}

.hardware-head {
    min-height: 42px;
    gap: 10px;
}

.hardware-main {
    min-width: 0;
    flex: 1;
}

.hardware-head strong {
    font-size: 14px !important;
}

.hardware-head small {
    font-size: 11px !important;
}

.hardware-head b {
    font-size: 10px !important;
    white-space: nowrap;
}

.partition-list {
    margin-top: 6px !important;
    padding-top: 6px !important;
}

.partition-row {
    padding: 3px 0 !important;
    min-height: 20px !important;
    font-size: 11px !important;
}

.partition-row em {
    font-size: 10px !important;
}

.storage-section {
    margin-bottom: 12px !important;
}

.storage-section-title {
    margin-bottom: 6px !important;
}
</style>

'@

if ($indexText -notmatch 'compact-device-panel') {

    $indexText = $indexText.Replace(
        "</head>",
        $compactCss + "</head>"
    )

    Write-Host "Added compact device styling." -ForegroundColor Green
}

# Try to identify the device panel and add an ID if needed.
if ($indexText -notmatch 'id="devices-panel"') {

    $drivePanelPattern = '(?is)<([a-z0-9]+)([^>]*class="[^"]*(?:storage|devices|drives)[^"]*"[^>]*)>'

    if ($indexText -match $drivePanelPattern) {

        $indexText = [regex]::Replace(
            $indexText,
            $drivePanelPattern,
            {
                param($m)

                $tag = $m.Groups[1].Value
                $attrs = $m.Groups[2].Value

                if ($attrs -match 'id=') {
                    return $m.Value
                }

                return "<$tag id=`"devices-panel`"$attrs>"
            },
            1
        )

        Write-Host "Added devices-panel ID." -ForegroundColor Green
    }
    else {
        Write-Host "Could not automatically identify the device panel." -ForegroundColor Yellow
        Write-Host "The JS visibility logic will still look for #drives-box / #drives." -ForegroundColor Yellow
    }
}

Set-Content -Path $index -Value $indexText -Encoding UTF8

# ------------------------------------------------------------
# Validation
# ------------------------------------------------------------

Write-Host ""
Write-Host "Checking files..." -ForegroundColor Cyan

$appCheck = Get-Content $app -Raw
$indexCheck = Get-Content $index -Raw

$checks = @(
    @("createPartitionRow", $appCheck.Contains("function createPartitionRow")),
    @("createHardwareCard", $appCheck.Contains("function createHardwareCard")),
    @("updateDevicesPanelVisibility", $appCheck.Contains("function updateDevicesPanelVisibility")),
    @("compact-device-panel", $indexCheck.Contains("compact-device-panel"))
)

foreach ($check in $checks) {
    if ($check[1]) {
        Write-Host "  OK  $($check[0])" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL $($check[0])" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "========================================"
Write-Host "UI update completed." -ForegroundColor Green
Write-Host "========================================"
Write-Host ""
Write-Host "Backup:"
Write-Host $backup
Write-Host ""
Write-Host "Now refresh the web UI with:"
Write-Host "Ctrl + Shift + R"
Write-Host ""
