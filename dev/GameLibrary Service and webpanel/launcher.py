import logging
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "launcher.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("gamelibrary.launcher")


def main():
    log.info("========== Game Library launcher.py ==========")
    log.info("Python: %s", sys.executable)
    log.info("Base directory: %s", BASE_DIR)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [sys.executable, "-m", "tray.tray"]
    log.debug("Launching tray command: %r", command)

    process = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        creationflags=flags
    )

    log.info("Tray process started: pid=%s", process.pid)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
