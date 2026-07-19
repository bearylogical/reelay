import os
from pathlib import Path


# Set Projects Root Directory
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.normpath(__file__)))

# Set Projects Configuration path
CONFIG_PATH = os.path.join(ROOT_DIR, "config.yaml")
CONFIG_EXAMPLE_PATH = os.path.join(ROOT_DIR, "config_example.yaml")
LANG_PATH = os.path.join(ROOT_DIR, "translations/")
CHATID_PATH = os.path.join(ROOT_DIR, "chatid.txt")
LOG_PATH = os.path.join(ROOT_DIR, "logs", "reelay.log")
ADMIN_PATH = os.path.join(ROOT_DIR, "admin.txt")
ALLOWLIST_PATH = os.path.join(ROOT_DIR, "allowlist.txt")
DB_PATH = os.path.join(ROOT_DIR, "reelay.db")
MINIAPP_HTML_PATH = os.path.join(ROOT_DIR, "templates", "miniapp.html")

DEFAULT_SETTINGS = {
    "entrypointAuth": "auth", #auth or a custom entrypoint
    "entrypointAdd": "start", #start or a custom entrypoint
    "entrypointDelete": "delete", #start or a custom entrypoint
    "entrypointAllSeries": "allSeries", #allSeries or a custom entrypoint
    "entrypointAllMovies": "allMovies", #allSeries or a custom entrypoint
    "entrypointTransmission": "transmission", #transmission or a custom entrypoint
    "entrypointSabnzbd": "sabnzbd", #sabnzbd or a custom entrypoint
    "logToConsole": True,
    "debugLogging": False,
    "language": "en-us",
    "transmission": { "enable": False },
    "enableAdmin": False,
    "overseerr": { "enable": False, "url": "", "apikey": "", "webhookSecret": "" },
    "reminderDefaultDays": 3,
    "reminderCheckHour": 9,
    "miniapp": { "enable": False, "url": "", "listenHost": "0.0.0.0", "listenPort": 8080 },
    "weeklyDigest": { "enable": False, "day": "monday", "hour": 9 },
}
