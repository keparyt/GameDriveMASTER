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

LOG_FILE = BASE_DIR / "logs" / "service.log"

URL = "http://127.0.0.1:8765"

BACKEND_PROCESS = None


def make_icon():
    image = Image.new(
        "RGBA",
        (64, 64),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (8, 8, 56, 56),
        radius=12,
        fill=(45, 120, 220, 255)
    )

    draw.rectangle(
        (18, 20, 46, 43),
        fill=(255, 255, 255, 255)
    )

    draw.rectangle(
        (23, 16, 41, 22),
        fill=(255, 255, 255, 255)
    )

    return image


def service_running():
    try:
        response = requests.get(
            URL + "/api/health",
            timeout=0.5
        )

        return response.ok

    except requests.RequestException:
        return False


def start_backend():
    global BACKEND_PROCESS

    if service_running():
        return True

    if (
        BACKEND_PROCESS is not None
        and BACKEND_PROCESS.poll() is None
    ):
        return False

    creation_flags = getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0
    )

    BACKEND_PROCESS = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "service.service"
        ],
        cwd=str(BASE_DIR),
        creationflags=creation_flags,

        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    for _ in range(40):

        if service_running():
            return True

        if BACKEND_PROCESS.poll() is not None:
            return False

        time.sleep(0.25)

    return service_running()


def open_interface(icon, item):
    webbrowser.open(URL)


def show_logs(icon, item):
    LOG_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    LOG_FILE.touch(
        exist_ok=True
    )

    os.startfile(
        str(LOG_FILE)
    )


def open_logs_folder(icon, item):
    LOG_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    os.startfile(
        str(LOG_FILE.parent)
    )


def restart_service(icon, item):
    global BACKEND_PROCESS

    if (
        BACKEND_PROCESS is not None
        and BACKEND_PROCESS.poll() is None
    ):
        try:
            BACKEND_PROCESS.terminate()
            BACKEND_PROCESS.wait(
                timeout=3
            )
        except Exception:
            try:
                BACKEND_PROCESS.kill()
            except Exception:
                pass

    BACKEND_PROCESS = None

    time.sleep(0.5)

    start_backend()


def open_data(icon, item):
    data_dir = BASE_DIR / "data"

    data_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    os.startfile(
        str(data_dir)
    )


def open_config(icon, item):
    os.startfile(
        str(BASE_DIR / "config.json")
    )


def exit_app(icon, item):
    global BACKEND_PROCESS

    if (
        BACKEND_PROCESS is not None
        and BACKEND_PROCESS.poll() is None
    ):
        try:
            BACKEND_PROCESS.terminate()
        except Exception:
            pass

    icon.stop()


def main():

    start_backend()

    menu = pystray.Menu(

        pystray.MenuItem(
            "Open Game Library",
            open_interface,
            default=True
        ),

        pystray.MenuItem(
            "Show Logs",
            show_logs
        ),

        pystray.MenuItem(
            "Open Logs Folder",
            open_logs_folder
        ),

        pystray.MenuItem(
            "Restart Service",
            restart_service
        ),

        pystray.MenuItem(
            "Open Data Folder",
            open_data
        ),

        pystray.MenuItem(
            "Settings",
            open_config
        ),

        pystray.Menu.SEPARATOR,

        pystray.MenuItem(
            "Exit",
            exit_app
        )
    )

    icon = pystray.Icon(
        "GameLibrary",
        make_icon(),
        "Game Library",
        menu
    )

    icon.run()


if __name__ == "__main__":
    main()
