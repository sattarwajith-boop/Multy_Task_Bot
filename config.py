"""Editable unified bot configuration.

Precedence:
    1. Koyeb / system environment variables
    2. Values in this file

For a public GitHub repository, leave secrets as empty strings here and put
them in Koyeb Secrets. For a private/local deployment, you may edit them here.
All values are strings because WZML-X reads its configuration from os.environ.
"""

BOT_TOKEN = ""
OWNER_ID = ""
TELEGRAM_API = ""
TELEGRAM_HASH = ""
DATABASE_URL = ""

LEECH_LOG_ID = ""
AUTHORIZED_CHATS = ""
SUDO_USERS = ""

BASE_URL = ""
BASE_URL_PORT = "8000"
PORT = "8000"
ENABLE_WEB_SERVER = "true"

DOWNLOAD_DIR = "/usr/src/app/downloads/"
DEFAULT_UPLOAD = "gd"

AUTO_MONITOR_ENABLED = "true"
AUTO_MONITOR_INTERVAL = "900"
AUTO_MONITOR_CHAT = ""
AUTO_MAX_ITEMS_PER_SITE = "10"
AUTO_LEECH_EXISTING = "false"
AUTO_FORWARD_CHATS = ""
MV_SITE_URL = ""
OMDB_API_KEY = ""

BOT_MAX_TASKS = "2"
QUEUE_ALL = "2"
TELEGRAM_WORKERS = "32"
TELEGRAM_TRANSMISSIONS = "8"
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
