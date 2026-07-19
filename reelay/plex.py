"""Plex.tv PIN-based OAuth ("Sign in with Plex") -- the same flow Overseerr's
own login page runs client-side. We run it server-side since the Mini App's
webview inside Telegram can't host Plex's auth popup."""

import logging
import urllib.parse
import uuid

import requests

from . import logger
from .config import config

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.plex", logLevel, config.get("logToConsole", False))

PRODUCT = "Reelay"
# Generated once per process. Pins live only a few minutes, so this doesn't
# need to survive a restart -- an in-flight link just needs retrying.
CLIENT_IDENTIFIER = uuid.uuid4().hex

_PINS_URL = "https://plex.tv/api/v2/pins"


def _headers():
    return {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
    }


def createPin():
    """Create a Plex auth PIN. Returns {"id", "code"} or None on failure."""
    try:
        resp = requests.post(_PINS_URL, headers=_headers(), params={"strong": "true"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {"id": data["id"], "code": data["code"]}
    except Exception as e:
        logger.warning(f"Plex createPin failed: {e}")
        return None


def authUrl(pin_code):
    params = {
        "clientID": CLIENT_IDENTIFIER,
        "code": pin_code,
        "context[device][product]": PRODUCT,
    }
    return "https://app.plex.tv/auth#?" + urllib.parse.urlencode(params)


def pollPin(pin_id):
    """Returns the Plex authToken once the user has authorized the PIN, or
    None if it's still pending (or the lookup failed)."""
    try:
        resp = requests.get(f"{_PINS_URL}/{pin_id}", headers=_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("authToken") or None
    except Exception as e:
        logger.warning(f"Plex pollPin failed for {pin_id}: {e}")
        return None
