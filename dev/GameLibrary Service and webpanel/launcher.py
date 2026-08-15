import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(
    __file__
).resolve().parent


def main():

    flags = getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0
    )

    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tray.tray"
        ],
        cwd=str(BASE_DIR),
        creationflags=flags
    )


if __name__ == "__main__":
    main()
