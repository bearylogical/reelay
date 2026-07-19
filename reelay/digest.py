"""Weekly "what's new" digest.

Reads the media_events recorded by the Overseerr webhook over the past week
and surfaces them two ways:
  * group   -- a library-wide "what's new" posted into each scope's #updates
               topic (the `updates` channel route);
  * personal -- a DM to each linked member listing the items THEY requested
               that became available, matched by Overseerr email (or username).

Each scope picks its own day/hour for the group post (and whether it's on at
all) via the Mini App -- see db.setWeeklyDigestConfig(). `weekly_digest_tick`
runs hourly and dispatches to whichever scopes are due; `enabled()` is just
the master switch for whether that hourly job runs at all.
"""

import logging
import types
from datetime import datetime

from . import channels
from . import db
from . import logger
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.digest", logLevel, config.get("logToConsole", False))

MAX_LINES = 40  # keep a digest well under Telegram's 4096-char message limit


def enabled():
    return bool(config.get("weeklyDigest", {}).get("enable"))


def _icon(media_type):
    return "📺" if media_type == "tv" else "🎬"


def _dedupe(events):
    seen, out = set(), []
    for e in events:
        key = (e.get("title"), e.get("media_type"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _render(events):
    shown = events[:MAX_LINES]
    lines = "\n".join(f"{_icon(e.get('media_type'))} {e.get('title')}" for e in shown)
    extra = len(events) - len(shown)
    if extra > 0:
        lines += "\n" + i18n.t("reelay.Digest.More", count=extra)
    return lines


def _matches(event, link):
    email = (event.get("requested_by_email") or "").lower()
    username = event.get("requested_by_username") or ""
    link_email = (link.get("seerr_email") or "").lower()
    link_user = link.get("seerr_username") or ""
    return bool((email and email == link_email) or (username and username == link_user))


def weekly_breakdown(events):
    """TV-vs-movie split of this week's (deduped) events, for the Mini App's
    "this week" view: {"counts": {"movie": n, "tv": m}, "movies": [...titles], "tv": [...titles]}."""
    unique = _dedupe(events)
    movies = [e.get("title") for e in unique if e.get("media_type") == "movie"]
    tv = [e.get("title") for e in unique if e.get("media_type") == "tv"]
    return {"counts": {"movie": len(movies), "tv": len(tv)}, "movies": movies, "tv": tv}


async def send_weekly_digest_to_scope(context, scope, events):
    """Post the group "what's new" into `scope`'s #updates topic and DM each
    linked member the items THEY requested that became available. `events`
    is the shared (not-yet-deduped) media_events list for the whole run --
    callers fetch it once and pass it to every due scope."""
    unique = _dedupe(events)
    if not unique:
        return
    group_text = i18n.t("reelay.Digest.GroupHeader", count=len(unique)) + "\n" + _render(unique)
    shim = types.SimpleNamespace(bot=context.bot)

    # Group "what's new" into the scope's #updates topic (plain text --
    # media titles must not be interpreted as Markdown).
    await channels.announce(
        shim, scope["chat_id"], channels.CATEGORY_UPDATES, group_text, parse_mode=None
    )
    # Personalized DMs.
    dmed = set()
    for m in db.getApprovedMembers(scope["chat_id"]):
        uid = m["user_id"]
        if uid in dmed:
            continue
        link = db.getSeerrLink(scope["chat_id"], uid)
        if not link:
            continue
        mine = _dedupe([e for e in events if _matches(e, link)])
        if not mine:
            continue
        text = i18n.t("reelay.Digest.PersonalHeader", count=len(mine)) + "\n" + _render(mine)
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
            dmed.add(uid)
        except Exception:
            logger.warning(f"Could not DM weekly digest to {uid}.")


async def weekly_digest_tick(context):
    """Hourly job (see enabled()/bot.py): checks which scopes are due for
    their weekly digest right now (their own day-of-week + hour, not yet
    sent today) and sends to each. Scheduling is per-scope (set in the Mini
    App); this is just the dispatcher."""
    now = datetime.now()
    day_name = now.strftime("%A").lower()
    today_str = now.date().isoformat()
    due = db.getScopesDueForWeeklyDigest(day_name, now.hour, today_str)
    if not due:
        return

    events = db.getRecentMediaEvents(7)
    if not events:
        db.pruneMediaEvents(30)
        return

    for scope in due:
        await send_weekly_digest_to_scope(context, scope, events)
        db.markWeeklyDigestSent(scope["chat_id"], today_str)

    db.pruneMediaEvents(30)
