"""Webhook receivers for everything that can put media in the library.

We don't post per-event -- we RECORD events, and the weekly digest job
(digest.py) surfaces them as a group "what's new" plus personalized DMs.

Overseerr alone isn't enough: anything added straight in Radarr/Sonarr, or
imported by hand, never produces an Overseerr notification, so it used to be
missing from the digest entirely. Each service below is optional and
independently configured; the digest merges and dedupes whatever arrives.

--- Overseerr ---------------------------------------------------------------
Configure in Overseerr → Settings → Notifications → Webhook:
  Webhook URL:  https://<your-reelay-host>/overseerr/webhook/<overseerr.webhookSecret>

Recommended JSON Payload (stable keys, no reliance on Overseerr's default):
{
  "notification_type": "{{notification_type}}",
  "subject": "{{subject}}",
  "media": { "media_type": "{{media_type}}", "status": "{{media_status}}",
             "tmdbId": "{{media_tmdbid}}" },
  "request": {
    "requestedBy_username": "{{requestedBy_username}}",
    "requestedBy_email": "{{requestedBy_email}}"
  }
}

--- Radarr / Sonarr ---------------------------------------------------------
Configure in Radarr/Sonarr → Settings → Connect → Webhook (payload is fixed
by the *arr, nothing to template). Tick "On Import" / "On File Import" and,
optionally, "On Movie Added" / "On Series Add":
  https://<your-reelay-host>/radarr/webhook/<radarr.webhookSecret>
  https://<your-reelay-host>/sonarr/webhook/<sonarr.webhookSecret>

An import means the file is on disk and watchable, so it records as
'available' -- the same footing as Overseerr's MEDIA_AVAILABLE. An add only
means it's wanted, so it records as 'added' and the digest lists it under
"coming soon". Quality upgrades of something already in the library are not
new content and are skipped.
"""

import hmac
import logging
import types

from aiohttp import web

from . import channels
from . import db
from . import digest
from . import logger
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.webhooks", logLevel, config.get("logToConsole", False))


SERVICES = ("overseerr", "radarr", "sonarr")


def _secret(service):
    # `or {}` and not a .get default: a YAML block written as bare "radarr:"
    # parses to None, which would blow up on .get().
    return (config.get(service) or {}).get("webhookSecret") or ""


def enabled():
    """True if any receiver is configured -- gates starting the HTTP server."""
    return any(_secret(s) for s in SERVICES)


def _authorized(request, service):
    secret = _secret(service)
    provided = request.match_info.get("secret", "")
    return bool(secret) and hmac.compare_digest(provided, secret)


async def _announce_test(request, message_key):
    """Echo a service's "Test" button into every scope's #updates topic, so an
    admin can confirm the wiring end to end without waiting a week."""
    shim = types.SimpleNamespace(bot=request.app["bot"])
    for scope in db.getActiveScopes():
        await channels.announce(shim, scope["chat_id"], channels.CATEGORY_UPDATES,
                                i18n.t(message_key))


async def handle_overseerr(request):
    # Unknown/missing secret looks like a non-existent endpoint on purpose.
    if not _authorized(request, "overseerr"):
        logger.warning("Rejected Overseerr webhook: missing or incorrect secret")
        raise web.HTTPNotFound()
    try:
        payload = await request.json()
    except Exception:
        logger.warning("Overseerr webhook: invalid JSON payload")
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    ntype = payload.get("notification_type", "")

    # The "Test" button in Overseerr -- confirm wiring by echoing into updates.
    if ntype == "TEST_NOTIFICATION":
        logger.info("Overseerr webhook: TEST_NOTIFICATION received")
        await _announce_test(request, "reelay.Updates.Connected")
        return web.json_response({"ok": True})

    # Record availability for the weekly digest; ignore everything else.
    if ntype == "MEDIA_AVAILABLE":
        media = payload.get("media") or {}
        req = payload.get("request") or {}
        title = payload.get("subject") or "Media"
        media_type = media.get("media_type")
        db.recordMediaEvent(
            title=title,
            media_type=media_type,
            requested_by_username=req.get("requestedBy_username"),
            requested_by_email=req.get("requestedBy_email"),
            source="overseerr",
            event=digest.EVENT_AVAILABLE,
            external_id=digest.external_id("tmdb", media.get("tmdbId")),
        )
        logger.info(f"Recorded media_event: {title} ({media_type}) available via overseerr")
    else:
        # INFO, not DEBUG: webhook volume is low and this is the only trace of
        # "Overseerr called us but we didn't record anything" -- worth seeing
        # by default, e.g. to confirm whether Overseerr sent MEDIA_AVAILABLE
        # at all for a given item.
        logger.info(f"Overseerr webhook: ignored notification_type={ntype!r} (subject={payload.get('subject')!r})")
    return web.json_response({"ok": True})


# --- Radarr / Sonarr ---------------------------------------------------------
#
# Both speak the same webhook shape: an `eventType` plus a `movie` or `series`
# object. Import events mean the file landed and is watchable; the *Added
# events only mean it's now wanted. Anything else (Grab, Rename, Health,
# Delete, ...) is logged and dropped, the same as Overseerr's other types.

_ARR = {
    "radarr": {
        "subject": "movie",
        "media_type": "movie",
        "id_field": "tmdbId",
        "id_namespace": "tmdb",
        "available_events": {"download", "moviefileimported", "import"},
        "added_events": {"movieadded"},
    },
    "sonarr": {
        "subject": "series",
        "media_type": "tv",
        "id_field": "tvdbId",
        "id_namespace": "tvdb",
        "available_events": {"download", "import"},
        "added_events": {"seriesadd", "seriesadded"},
    },
}


async def _handle_arr(request, service):
    spec = _ARR[service]
    if not _authorized(request, service):
        logger.warning(f"Rejected {service.title()} webhook: missing or incorrect secret")
        raise web.HTTPNotFound()
    try:
        payload = await request.json()
    except Exception:
        logger.warning(f"{service.title()} webhook: invalid JSON payload")
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    etype = (payload.get("eventType") or "").strip()
    key = etype.lower()

    if key == "test":
        logger.info(f"{service.title()} webhook: Test received")
        await _announce_test(request, "reelay.Updates.ArrConnected")
        return web.json_response({"ok": True})

    if key in spec["available_events"]:
        # A quality upgrade replaces a file that was already watchable -- it is
        # not new content, and announcing it would be noise.
        if payload.get("isUpgrade"):
            logger.info(f"{service.title()} webhook: ignored {etype!r} (quality upgrade)")
            return web.json_response({"ok": True})
        event = digest.EVENT_AVAILABLE
    elif key in spec["added_events"]:
        event = digest.EVENT_ADDED
    else:
        logger.info(f"{service.title()} webhook: ignored eventType={etype!r}")
        return web.json_response({"ok": True})

    subject = payload.get(spec["subject"]) or {}
    title = subject.get("title")
    if not title:
        # Without a title there's nothing to put in the digest, and an
        # untitled row would dedupe against every other untitled row.
        logger.warning(f"{service.title()} webhook: {etype!r} had no {spec['subject']}.title, skipped")
        return web.json_response({"ok": False, "error": "no_title"}, status=400)

    db.recordMediaEvent(
        title=title,
        media_type=spec["media_type"],
        source=service,
        event=event,
        external_id=digest.external_id(spec["id_namespace"], subject.get(spec["id_field"])),
    )
    logger.info(f"Recorded media_event: {title} ({spec['media_type']}) {event} via {service}")
    return web.json_response({"ok": True})


async def handle_radarr(request):
    return await _handle_arr(request, "radarr")


async def handle_sonarr(request):
    return await _handle_arr(request, "sonarr")
