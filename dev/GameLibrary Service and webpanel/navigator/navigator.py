import sys
import time
import webbrowser

import requests


URL = "http://127.0.0.1:8765"


def main():

    try:
        response = requests.get(
            URL + "/api/health",
            timeout=2
        )

        if not response.ok:
            raise RuntimeError(
                "Service returned an invalid response."
            )

    except Exception:

        print(
            "Game Library service is not running."
        )

        print(
            "Start launcher.py or start_hidden.vbs first."
        )

        input(
            "\nPress Enter to close..."
        )

        return 1

    webbrowser.open(URL)

    return 0


if __name__ == "__main__":
    sys.exit(main())
