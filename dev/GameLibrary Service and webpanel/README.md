# Game Library

A Windows-local GameDrive library scanner with:

- persistent SQLite database
- removable-drive detection
- GameDrive identification
- automatic game scanning
- FastAPI local API
- browser-based game navigator
- system tray application
- hidden backend process
- local logs
- drive connection/offline state
- metadata abstraction
- artwork fields
- automatic cleanup of games removed from a drive

---

## Architecture

```text
                    Windows
                       │
                       ▼
              ┌─────────────────┐
              │ launcher.py     │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  tray.tray      │
              │                 │
              │  System Tray    │
              └────────┬────────┘
                       │
                       │ starts
                       ▼
              ┌─────────────────┐
              │ service.service │
              └────────┬────────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Scanner      SQLite       FastAPI
          │            │            │
          │            │            ▼
          │            │         Browser
          │            │
          ▼            ▼
     GameDrive      Database
````

---

# Installation

Open PowerShell:

```powershell
cd D:\dev\GameLibrary

py -m pip install -r requirements.txt
```

---

# Start normally

```powershell
py launcher.py
```

The tray application starts.

The backend is started automatically.

The console is hidden.

---

# Start completely hidden

Double-click:

```text
start_hidden.vbs
```

The VBS launcher starts Python without opening a console window.

---

# Web interface

Open:

```text
http://127.0.0.1:8765
```

---

# GameDrive structure

The scanner expects drives to look like:

```text
D:\
│
├── GameDrive.ini
│
└── Cracked\
    │
    ├── Game 1\
    ├── Game 2\
    ├── Game 3\
    └── Game 4\
```

Example:

```ini
[Drive]
name=My GameDrive
description=My external game collection
```

---

# Drive identification

The scanner attempts to identify the physical disk using:

1. physical disk serial
2. volume serial

The resulting identifier is stored in the database.

Example:

```text
DISK:ABC123|VOL:8F21A992
```

This means reconnecting the same physical drive under another letter can still
identify it as the same GameDrive.

---

# Offline drives

Disconnecting a drive does NOT delete it.

Instead:

```text
CONNECTED
    │
    │ unplug
    ▼
OFFLINE
```

When it is reconnected:

```text
OFFLINE
    │
    │ reconnect
    ▼
CONNECTED
```

The database keeps the library entry.

---

# Game scanning

Every scan:

1. detects Windows drives
2. searches for GameDrive.ini
3. identifies the drive
4. reads the drive information
5. scans the Cracked folder
6. updates existing games
7. adds new games
8. removes games that no longer exist on the connected drive

The scan interval is configured in:

```text
config.json
```

Default:

```json
{
    "scan_interval_seconds": 10
}
```

---

# Database

The database is:

```text
data/database.db
```

It contains:

```text
drives
games
metadata
```

The database uses SQLite WAL mode.

---

# Metadata

Metadata is intentionally separated from scanning.

The metadata layer can later connect to a permitted provider/API.

The database already supports:

```text
title
app_id
capsule
logo
hero
cover
release_date
description
```

Artwork can eventually be cached inside:

```text
data/images/
```

---

# API

Health:

```text
GET /api/health
```

Games:

```text
GET /api/games
```

Search:

```text
GET /api/games?q=minecraft
```

Connected drives only:

```text
GET /api/games?connected_only=true
```

Individual game:

```text
GET /api/games/1
```

---

# Logs

Logs are stored in:

```text
logs/service.log
```

The system tray has:

```text
Show Logs
Open Logs Folder
```

---

# System tray

The tray menu provides:

```text
Open Game Library
Show Logs
Open Logs Folder
Restart Service
Open Data Folder
Settings
Exit
```

---

# Important Windows architecture detail

The backend and tray are intentionally separate.

```text
Tray
  │
  └── Backend
       │
       ├── Scanner
       ├── SQLite
       └── FastAPI
```

This is because a real Windows Service normally runs in Session 0 and should
not directly own an interactive user tray icon.

If machine-wide service functionality is needed later, the backend can be
converted into a proper Windows Service while keeping the tray as the
interactive companion.

---

# Development

Backend:

```powershell
py -m service.service
```

Tray:

```powershell
py -m tray.tray
```

Navigator:

```powershell
py -m navigator.navigator
```

---

# Project structure

```text
GameLibrary/
│
├── config.json
├── launcher.py
├── start_hidden.vbs
├── requirements.txt
├── README.md
│
├── service/
│   ├── __init__.py
│   ├── api.py
│   ├── database.py
│   ├── metadata.py
│   ├── scanner.py
│   └── service.py
│
├── tray/
│   ├── __init__.py
│   └── tray.py
│
├── navigator/
│   ├── __init__.py
│   └── navigator.py
│
├── web/
│   ├── index.html
│   ├── style.css
│   └── app.js
│
├── data/
│   ├── database.db
│   └── images/
│
└── logs/
    └── service.log
```

