import logging
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import pystray
import requests
from PIL import Image, ImageDraw


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "service.log"
URL = "http://127.0.0.1:8765"
BACKEND_PROCESS = None

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("gamelibrary.tray")


def make_icon():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(45, 120, 220, 255))
    draw.rectangle((18, 20, 46, 43), fill=(255, 255, 255, 255))
    draw.rectangle((23, 16, 41, 22), fill=(255, 255, 255, 255))
    return image


def service_running():
    try:
        response = requests.get(URL + "/api/health", timeout=0.5)
        log.debug("Health check: HTTP %s body=%s", response.status_code, response.text[:500])
        return response.ok
    except requests.RequestException as exc:
        log.debug("Health check failed: %s", exc)
        return False


def start_backend():
    global BACKEND_PROCESS
    log.info("Starting Game Library backend")

    if service_running():
        log.info("Backend is already running")
        return True

    if BACKEND_PROCESS is not None and BACKEND_PROCESS.poll() is None:
        log.info("Backend process already exists; waiting for API")
    else:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        backend_log = LOG_DIR / "backend.log"
        backend_handle = open(backend_log, "a", encoding="utf-8", buffering=1)
        BACKEND_PROCESS = subprocess.Popen(
            [sys.executable, "-m", "service.service"],
            cwd=str(BASE_DIR),
            creationflags=creation_flags,
            stdin=subprocess.DEVNULL,
            stdout=backend_handle,
            stderr=subprocess.STDOUT
        )
        log.info("Backend process started: pid=%s log=%s", BACKEND_PROCESS.pid, backend_log)

    # The web API must become available before waiting for any metadata/artwork.
    for attempt in range(120):
        if service_running():
            log.info("Web panel/API is ready after %.1fs", attempt * 0.25)
            return True

        if BACKEND_PROCESS is not None and BACKEND_PROCESS.poll() is not None:
            log.error("Backend exited during startup with code %s", BACKEND_PROCESS.returncode)
            return False

        time.sleep(0.25)

    log.error("Backend did not become ready within 30 seconds")
    return service_running()


def open_interface(icon, item):
    log.info("Opening web panel: %s", URL)
    webbrowser.open(URL)


def show_logs(icon, item):
    LOG_FILE.touch(exist_ok=True)
    os.startfile(str(LOG_FILE))


def open_logs_folder(icon, item):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(LOG_DIR))


def restart_service(icon, item):
    global BACKEND_PROCESS
    log.info("Restart requested")
    if BACKEND_PROCESS is not None and BACKEND_PROCESS.poll() is None:
        try:
            BACKEND_PROCESS.terminate()
            BACKEND_PROCESS.wait(timeout=3)
        except Exception:
            log.exception("Graceful backend termination failed")
            try:
                BACKEND_PROCESS.kill()
            except Exception:
                log.exception("Backend kill failed")
    BACKEND_PROCESS = None
    time.sleep(0.5)
    start_backend()


def open_data(icon, item):
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.startfile(str(data_dir))


def open_config(icon, item):
    os.startfile(str(BASE_DIR / "config.json"))


def exit_app(icon, item):
    global BACKEND_PROCESS
    log.info("Exiting Game Library tray")
    if BACKEND_PROCESS is not None and BACKEND_PROCESS.poll() is None:
        try:
            BACKEND_PROCESS.terminate()
        except Exception:
            log.exception("Failed to terminate backend")
    icon.stop()


def main():
    log.info("========== Game Library launcher starting ==========")
    log.info("Base directory: %s", BASE_DIR)
    start_backend()

    menu = pystray.Menu(
        pystray.MenuItem("Open Game Library", open_interface, default=True),
        pystray.MenuItem("Show Logs", show_logs),
        pystray.MenuItem("Open Logs Folder", open_logs_folder),
        pystray.MenuItem("Restart Service", restart_service),
        pystray.MenuItem("Open Data Folder", open_data),
        pystray.MenuItem("Settings", open_config),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_app)
    )

    icon = pystray.Icon("GameLibrary", make_icon(), "Game Library", menu)
    icon.run()


if __name__ == "__main__":
    main()
