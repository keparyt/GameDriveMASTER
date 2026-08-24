# Private game-analysis workflow

## Channel messages

Discord does not support ephemeral responses to ordinary Message objects. The automatic game detector is triggered by a normal channel message, so the bot must first establish a private response before deleting the user's channel message.

## DM workflow

When a user sends a supported game/media input directly to the bot by DM, do not automatically assume that every DM is intended for game parsing.

The bot should first send a confirmation prompt with two buttons:

- **Yes — Parse Games**: run the normal game-media analysis pipeline on the DM content/media.
- **No**: do not parse it as a game. If the DM is recognized as another supported bot command/workflow, allow that workflow to execute normally. Otherwise ignore the DM after the confirmation is declined.

The confirmation should be private to the user and should expire after a reasonable timeout. Once the user chooses **Yes**, disable the confirmation controls and begin the normal analysis workflow.

## Accuracy disclaimer

Every game-analysis result panel should clearly state that the detector is **not 100% perfect**. Identification is based on available media, OCR, metadata and verification sources, and users should review the detected games before adding them to the library queue.

## Selection lifetime

The private game-selection panel remains usable for up to 24 hours, or is removed immediately once the user has selected/resolved all verified games. Interaction responses remain ephemeral where Discord supports them.
