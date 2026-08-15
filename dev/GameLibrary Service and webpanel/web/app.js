const grid = document.querySelector("#grid");
const search = document.querySelector("#search");
const online = document.querySelector("#online");
const errorBox = document.querySelector("#error");
const drivesBox = document.querySelector("#drives");
const driveCount = document.querySelector("#drive-count");

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[character]));
}
function showError(message) { errorBox.textContent = message; errorBox.style.display = "block"; }
function hideError() { errorBox.textContent = ""; errorBox.style.display = "none"; }

function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (!value) return "—";
    const units = ["B", "GB", "TB", "PB"];
    let size = value;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
    return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

function createDriveCard(drive) {
    const connected = Boolean(drive.connected);
    const letter = drive.last_letter ? `${drive.last_letter}:` : "—";
    return `<div class="drive-chip ${connected ? "connected" : "disconnected"}">
        <span class="drive-dot"></span>
        <div class="drive-chip-info">
            <strong>${escapeHtml(drive.name || "Unnamed GameDrive")}</strong>
            <small>${escapeHtml(letter)} · ${connected ? "Connected" : "Offline"}</small>
        </div>
    </div>`;
}

function createHardwareCard(disk) {
    const onlineDisk = !disk.offline && String(disk.status).toLowerCase().includes("online");
    return `<div class="hardware-chip ${onlineDisk ? "connected" : "disconnected"}">
        <span class="hardware-icon">▣</span>
        <div class="drive-chip-info">
            <strong>${escapeHtml(disk.name || "Unknown disk")}</strong>
            <small>${escapeHtml(disk.bus || "Unknown")} · ${escapeHtml(formatBytes(disk.size))} · ${escapeHtml(disk.health || "Unknown")}</small>
        </div>
    </div>`;
}

async function loadDrives() {
    const [driveResponse, hardwareResponse] = await Promise.all([
        fetch("/api/drives"),
        fetch("/api/system/disks")
    ]);
    if (!driveResponse.ok || !hardwareResponse.ok) throw new Error("Unable to load drive information");

    const drives = await driveResponse.json();
    const hardware = await hardwareResponse.json();
    const connected = drives.filter(drive => Boolean(drive.connected)).length;

    driveCount.textContent = `${connected} connected / ${drives.length} collections`;

    const collections = drives.map(createDriveCard).join("");
    const hardwareCards = hardware.map(createHardwareCard).join("");
    drivesBox.innerHTML = `
        <div class="drive-group">
            <div class="drive-group-title">GameDrive collections</div>
            <div class="drive-list">${collections || `<span class="muted">No GameDrive collections indexed yet.</span>`}</div>
        </div>
        <div class="drive-group">
            <div class="drive-group-title">Physical hardware</div>
            <div class="drive-list">${hardwareCards || `<span class="muted">No physical disks detected.</span>`}</div>
        </div>`;
}

function createCard(game) {
    const connected = Boolean(game.connected);
    const cover = game.cover || game.capsule;
    const logo = game.logo;
    const title = game.title || game.name || "Unknown Game";
    const drive = game.drive_name || "Unknown drive";
    const letter = connected && game.last_letter ? `${game.last_letter}:` : "Offline";

    return `<article class="card ${connected ? "" : "offline-card"}">
        <div class="art">
            ${cover ? `<img src="${escapeHtml(cover)}" alt="${escapeHtml(title)}" loading="lazy">` : `<div class="fallback">NO ARTWORK</div>`}
        </div>
        <div class="info">
            ${logo ? `<img class="logo" src="${escapeHtml(logo)}" alt="" loading="lazy">` : ""}
            <div class="title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
            <div class="drive">${escapeHtml(drive)} · ${escapeHtml(letter)}</div>
            <div class="badge ${connected ? "" : "offline"}">${connected ? "CONNECTED" : "OFFLINE"}</div>
        </div>
    </article>`;
}

async function loadGames() {
    const params = new URLSearchParams({ q: search.value.trim(), connected_only: online.checked });
    const response = await fetch(`/api/games?${params.toString()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data.length) { grid.innerHTML = `<div class="empty">No games found.</div>`; return; }
    grid.innerHTML = data.map(createCard).join("");
}

async function load() {
    hideError();
    try {
        // Load collections first, then games. A slow artwork lookup must not
        // replace the drive list or make another disk's library disappear.
        await loadDrives();
        await loadGames();
    } catch (error) {
        console.error(error);
        showError("Unable to load the complete Game Library.");
    }
}

let timer = null;
search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(loadGames, 150); });
online.addEventListener("change", loadGames);
load();
