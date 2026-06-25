"""Editable unified bot configuration.

Precedence:
    1. Koyeb / system environment variables
    2. Values in this file

For a public GitHub repository, leave secrets as empty strings here and put
them in Koyeb Secrets. For a private/local deployment, you may edit them here.
All values are strings because WZML-X reads its configuration from os.environ.
"""

BOT_TOKEN = ""          # Example: 1234567890:AA...
OWNER_ID = ""           # Your Telegram numeric user id
TELEGRAM_API = ""       # From https://my.telegram.org
TELEGRAM_HASH = ""      # From https://my.telegram.org
DATABASE_URL = ""       # MongoDB connection string

LEECH_LOG_ID = ""       # Upload/log group or channel id, usually -100...
AUTHORIZED_CHATS = ""   # Chat ids allowed to use the bot
SUDO_USERS = ""         # Sudo user ids

BASE_URL = ""           # Example: https://your-service-name.koyeb.app
BASE_URL_PORT = "8080"
PORT = "8080"
ENABLE_WEB_SERVER = "true"
SCRAPER_ONLY = "false"

DOWNLOAD_DIR = "/usr/src/app/downloads/"
DEFAULT_UPLOAD = "gd"

AUTO_MONITOR_ENABLED = "true"
AUTO_MONITOR_INTERVAL = "900"
AUTO_MONITOR_CHAT = ""  # Group/channel id where auto monitor posts/leech starts
AUTO_MAX_ITEMS_PER_SITE = "3"
AUTO_MAX_TASKS_PER_RUN = "2"
AUTO_DISPATCH_DELAY = "8"
AUTO_LEECH_EXISTING = "false"
AUTO_FETCH_RETRIES = "3"
AUTO_FETCH_TIMEOUT = "35"
AUTO_SITE_COOKIE = ""
AUTO_SITE_PROXY = ""
AUTO_FORWARD_CHATS = "" # Optional extra destination chats, space/comma separated
MV_SITE_URL = ""        # Example: https://www.example-site.com
OMDB_API_KEY = ""       # API key only, or full omdbapi.com URL

BOT_MAX_TASKS = "2"
QUEUE_ALL = "2"
TELEGRAM_WORKERS = "8"
TELEGRAM_TRANSMISSIONS = "2"
STATUS_UPDATE_INTERVAL = "10"
RSS_DELAY = "900"
INCOMPLETE_TASK_NOTIFIER = "true"
SET_COMMANDS = "true"
TIMEZONE = "Asia/Colombo"


def apply_config():
    """Load non-empty values without replacing real environment variables."""
    from os import environ

    ignored = {"apply_config"}
    for name, value in globals().items():
        if (
            name.startswith("_")
            or name in ignored
            or not name.isupper()
            or value is None
            or value == ""
        ):
            continue
        environ.setdefault(name, str(value))
