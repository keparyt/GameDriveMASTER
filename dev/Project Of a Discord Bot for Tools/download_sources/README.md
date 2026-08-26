# Local download sources

Place one or more `.json` source files in this directory (or configure `DOWNLOAD_SOURCES_DIR`).

Each file should contain:

```json
{
  "name": "SteamRip",
  "downloads": [
    {
      "title": "Example Game Free Download (v1.2.3)",
      "uploadDate": "2026-08-26T10:00:00+00:00",
      "fileSize": "4.5 GB",
      "uris": [
        "https://example.invalid/download/abc"
      ]
    }
  ]
}
```

`onlinefix.json` is automatically given highest source priority when present.

The bot indexes the files in memory and checks file modification time/size so a changed source is reloaded without restarting the bot. Invalid JSON files are logged and skipped instead of stopping the bot.

`uris` contains the actual download target(s). They are not treated as webpages. Valid HTTP(S)/FTP and `magnet:?` URIs are preserved, and the first URI is exposed as the queue's backwards-compatible `download_url`.
