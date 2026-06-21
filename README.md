# Unified Leech Bot

A single Telegram bot combining:

- WZML-X torrent, magnet, direct-link, Telegram, Google Drive, rclone and
  yt-dlp download/upload features
- MV-style torrent and magnet discovery
- MongoDB-backed website/RSS monitoring and duplicate prevention
- Automatic OMDb/IMDb cards
- Automatic copying to multiple Telegram destinations
- Koyeb-compatible HTTP health endpoint

Use the bot only for content you are authorized to download and redistribute.

## Deploy

1. Put the contents of this directory at the root of a private GitHub
   repository.
2. Create a Koyeb Web Service using the included `Dockerfile`.
3. Paste `KOYEB_ENV.txt` into Koyeb's environment editor and replace every
   `CHANGE_ME`.
4. Expose the same HTTP port as `PORT` (normally `8000`). Health path:
   `/health`.
5. Do not set a custom run command. The Docker image runs `bash start.sh`.

Keep Telegram, MongoDB and API credentials in Koyeb Secrets. Do not commit
them to `config.py`.

See `UNIFIED_SETUP.md` for commands and monitoring behavior.
