from datetime import datetime


def log(message: str):
    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(f"[{timestamp}] {message}")


def success(message: str):
    log(f"[+] {message}")


def warning(message: str):
    log(f"[!] {message}")


def error(message: str):
    log(f"[-] {message}")
