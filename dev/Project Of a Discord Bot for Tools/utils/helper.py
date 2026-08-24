from datetime import datetime


DEBUG = False


def set_debug(enabled: bool):
    global DEBUG
    DEBUG = enabled
    log(f"Debug logging {'enabled' if enabled else 'disabled'}")


def log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def debug(message: str):
    if DEBUG:
        log(f"[DEBUG] {message}")


def success(message: str):
    log(f"[+] {message}")


def warning(message: str):
    log(f"[!] {message}")


def error(message: str):
    log(f"[-] {message}")
