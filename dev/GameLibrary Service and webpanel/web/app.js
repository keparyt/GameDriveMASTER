const grid = document.querySelector("#grid");
const search = document.querySelector("#search");
const online = document.querySelector("#online");
const errorBox = document.querySelector("#error");

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
    }[character]));
}

function showError(message) {
    errorBox.textContent = message;
    errorBox.style.display = "block";
}

function hideError() {
    errorBox.textContent = "";
    errorBox.style.display = "none";
}

function createCard(game) {
    const hero = game.hero;
    const cover = game.cover || game.capsule;
    const logo = game.logo;
    const title = game.title || game.name || "Unknown Game";
    const drive = game.drive_name || "Unknown drive";
    const letter = game.last_letter || "?";
    const connected = Boolean(game.connected);

    return `
        <article class="card">
            <div class="art">
                ${hero ? `<img class="hero" src="${escapeHtml(hero)}" alt="" loading="lazy"
                    onerror="this.style.display='none'; this.nextElementSibling.style.display='block'">` : ""}
                ${cover ? `<img class="cover" src="${escapeHtml(cover)}" alt="" loading="lazy"
                    onerror="this.style.display='none'">` : ""}
                ${!hero && !cover ? `<div class="fallback">NO ARTWORK</div>` : ""}
            </div>
            <div class="info">
                ${logo ? `<img class="logo" src="${escapeHtml(logo)}" alt="${escapeHtml(title)}" loading="lazy">` : ""}
                <div class="title">${escapeHtml(title)}</div>
                <div class="drive">${escapeHtml(drive)} · ${escapeHtml(letter)}:</div>
                <div class="badge ${connected ? "" : "offline"}">${connected ? "CONNECTED" : "OFFLINE"}</div>
            </div>
        </article>
    `;
}

async function load() {
    hideError();
    try {
        const params = new URLSearchParams({
            q: search.value.trim(),
            connected_only: online.checked
        });
        const response = await fetch(`/api/games?${params.toString()}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!data.length) {
            grid.innerHTML = `<div class="empty">No games found.</div>`;
            return;
        }
        grid.innerHTML = data.map(createCard).join("");
    } catch (error) {
        console.error(error);
        grid.innerHTML = "";
        showError("Unable to connect to the Game Library service.");
    }
}

let timer = null;
search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(load, 150);
});
online.addEventListener("change", load);
load();
