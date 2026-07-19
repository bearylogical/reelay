"""Overseerr webhook receiver.

Overseerr's webhook notification agent POSTs a (user-configurable) JSON
payload on request/media events. We don't post per-event anymore -- instead
we RECORD `MEDIA_AVAILABLE` events, and the weekly digest job (digest.py)
surfaces them as a group "what's new" plus personalized DMs.

Configure in Overseerr → Settings → Notifications → Webhook:
  Webhook URL:  https://<your-reelay-host>/overseerr/webhook/<webhookSecret>

Recommended JSON Payload (stable keys, no reliance on Overseerr's default):
{
  "notification_type": "{{notification_type}}",
  "subject": "{{subject}}",
  "media": { "media_type": "{{media_type}}", "status": "{{media_status}}" },
  "request": {
    "requestedBy_username": "{{requestedBy_username}}",
    "requestedBy_email": "{{requestedBy_email}}"
  }
}
"""

import hmac
import logging
import types

from aiohttp import web

from . import channels
from . import db
from . import logger
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.webhooks", logLevel, config.get("logToConsole", False))


def enabled():
    return bool(config.get("overseerr", {}).get("webhookSecret"))


def _authorized(request):
    secret = config.get("overseerr", {}).get("webhookSecret") or ""
    provided = request.match_info.get("secret", "")
    return bool(secret) and hmac.compare_digest(provided, secret)


async def handle_overseerr(request):
    # Unknown/missing secret looks like a non-existent endpoint on purpose.
    if not _authorized(request):
        raise web.HTTPNotFound()
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    ntype = payload.get("notification_type", "")

    # The "Test" button in Overseerr -- confirm wiring by echoing into updates.
    if ntype == "TEST_NOTIFICATION":
        shim = types.SimpleNamespace(bot=request.app["bot"])
        for scope in db.getActiveScopes():
            await channels.announce(shim, scope["chat_id"], channels.CATEGORY_UPDATES,
                                    i18n.t("reelay.Updates.Connected"))
        return web.json_response({"ok": True})

    # Record availability for the weekly digest; ignore everything else.
    if ntype == "MEDIA_AVAILABLE":
        media = payload.get("media") or {}
        req = payload.get("request") or {}
        db.recordMediaEvent(
            title=payload.get("subject") or "Media",
            media_type=media.get("media_type"),
            requested_by_username=req.get("requestedBy_username"),
            requested_by_email=req.get("requestedBy_email"),
        )
    return web.json_response({"ok": True})
