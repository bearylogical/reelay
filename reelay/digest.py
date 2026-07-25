"""Weekly "what's new" digest.

Reads the media_events recorded over the past week and surfaces them two ways:
  * group   -- a library-wide "what's new" posted into each scope's #updates
               topic (the `updates` channel route), in two sections: what
               became watchable, and what was merely added (coming soon);
  * personal -- a DM to each linked member listing the items THEY requested
               that became available, matched by Overseerr email (or username).

Events come from every source that can put something in the library, not just
Overseerr -- see webhooks.py (Overseerr MEDIA_AVAILABLE, Radarr/Sonarr import
and add events) and bot.addSerieMovie (reelay's own /start add flow). Because
the same item can arrive from several of them, _dedupe() collapses by external
id first and normalized title second.

Those are all push sources, so a digest built only from them is empty until
every webhook is wired -- and stays empty for anything that happened while the
bot was restarting. collect_events() therefore also PULLS the current picture
from Overseerr (see overseerr_events()), which needs no webhook at all and is
the same well sendReminders() has always drawn from. The two merge through the
usual _dedupe(), so an item both saw is one line carrying both sources.

Each scope picks its own day/hour for the group post (and whether it's on at
all) via the Mini App -- see db.setWeeklyDigestConfig(). `weekly_digest_tick`
runs hourly and dispatches to whichever scopes are due; `enabled()` is just
the master switch for whether that hourly job runs at all.
"""

import logging
import re
import time
import types
from datetime import datetime, timedelta, timezone

from . import channels
from . import db
from . import logger
from . import overseerr
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.digest", logLevel, config.get("logToConsole", False))

MAX_LINES = 40  # keep a digest well under Telegram's 4096-char message limit

# media_events.event values -- 'available' means watchable now, 'added' means
# it's in the library but hasn't been downloaded yet.
EVENT_AVAILABLE = "available"
EVENT_ADDED = "added"

_YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)\s*$")
_WHITESPACE = re.compile(r"\s+")


def enabled():
    return bool(config.get("weeklyDigest", {}).get("enable"))


def _icon(media_type):
    if media_type == "tv":
        return "📺"
    if media_type == "movie":
        return "🎬"
    return "🍿"  # source didn't tell us


def external_id(namespace, value):
    """Build a dedupe id like 'tmdb:603' -- the one key that survives two
    sources spelling the same title differently. Returns None unless the value
    actually looks like an id. That check matters: Overseerr's payload is a
    user-edited template, and an unsubstituted '{{media_tmdbid}}' arriving as a
    literal string would become one id shared by every event, collapsing the
    whole digest into a single line."""
    text = str(value).strip() if value is not None else ""
    return f"{namespace}:{text}" if text.isdigit() and int(text) else None


def _norm(title):
    """Collapse the spellings different sources use for the same item --
    Overseerr's subject is typically "The Matrix (1999)", Radarr's title is
    "The Matrix"."""
    return _WHITESPACE.sub(" ", _YEAR_SUFFIX.sub("", (title or "").strip().lower()))


def _sources_of(event):
    """The set of sources an event is known to have come from. Already-deduped
    events carry a merged `sources`; raw rows carry a single `source`."""
    merged = event.get("sources")
    if merged:
        return set(merged)
    return {event["source"]} if event.get("source") else set()


def _duplicate_of(event, by_id, by_title):
    """The already-kept event this one duplicates, or None. Two events match
    when they share an external id, or -- since ids are often missing, and
    Radarr's tmdb and Sonarr's tvdb ids aren't comparable anyway -- when they
    share a normalized title of a compatible media type. A missing media_type
    matches any type, so a source that omits it can't cause the same item to
    be listed twice."""
    external_id = event.get("external_id")
    if external_id and external_id in by_id:
        return by_id[external_id]
    kept_by_type = by_title.get(_norm(event.get("title")))
    if not kept_by_type:
        return None
    media_type = event.get("media_type") or None
    if media_type is None:  # unknown type matches whatever we have for the title
        return next(iter(kept_by_type.values()))
    return kept_by_type.get(media_type) or kept_by_type.get(None)


def _dedupe(events):
    """One entry per distinct item, keeping the earliest. Returns copies, each
    with a `sources` set merged from every event that collapsed into it -- an
    item reported by both Overseerr and Radarr must show both, not just
    whichever webhook happened to fire first."""
    by_id, by_title, out = {}, {}, []
    for e in events:
        prior = _duplicate_of(e, by_id, by_title)
        if prior is not None:
            prior["sources"] |= _sources_of(e)
            continue
        kept = dict(e)
        kept["sources"] = _sources_of(e)
        out.append(kept)
        if kept.get("external_id"):
            by_id[kept["external_id"]] = kept
        by_title.setdefault(_norm(kept.get("title")), {})[kept.get("media_type") or None] = kept
    return out


# --- Overseerr poll -----------------------------------------------------------
#
# media_events only holds what something told us about, so a library whose
# webhooks were never wired produces an empty digest no matter how much was
# added. Overseerr already knows; we just ask.
#
# The catch is that a poll returns current STATE, not observed transitions, so
# "this week" has to come from a timestamp -- and which timestamp depends on
# the bucket. A film requested in April that landed yesterday is new this week;
# reading its createdAt would put it outside every window it will ever have,
# hiding it forever. Availability is dated by mediaAddedAt (when it landed),
# "coming soon" by createdAt (when it was asked for).

_POLL_SOURCE = "overseerr-poll"

# MediaInfo.status -- see overseerr._STATUS_BADGES for the full list.
_POLL_AVAILABLE = {4, 5}  # PARTIALLY_AVAILABLE, AVAILABLE
_POLL_PENDING = {2, 3}    # PENDING, PROCESSING -- wanted, not watchable yet
# MediaRequest.status: 3 DECLINED, 4 FAILED. Neither is ever going to arrive.
_POLL_DEAD_REQUEST = {3, 4}

# Requests are walked oldest-to-newest-agnostic (we can't stop early: an old
# request can have become available yesterday), so this bounds the walk on a
# long-lived install. Hitting it is logged, never silent.
POLL_MAX_REQUESTS = 1000

# The Mini App's "this week" view hits this on every open, and each call is a
# blocking HTTP round trip (plus a title lookup per new item). A short TTL
# keeps a burst of opens down to one poll; a weekly digest does not need
# second-fresh data.
POLL_TTL_SECONDS = 300
_poll_cache = {"at": 0.0, "days": None, "events": []}


def _parse_ts(value):
    """Overseerr's ISO-8601 timestamps as an aware UTC datetime, or None.
    Naive values are read as UTC -- Overseerr stores UTC throughout."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _poll_event(req, cutoff, title_cache):
    """One Overseerr request as a media_events-shaped dict, or None if it falls
    outside the window or has nothing worth announcing."""
    if req.get("status") in _POLL_DEAD_REQUEST:
        return None
    media = req.get("media") or {}
    status = media.get("status")
    if status in _POLL_AVAILABLE:
        event = EVENT_AVAILABLE
        # updatedAt is a fallback, not a preference: it moves on any edit, so
        # it can re-announce an old item. Better one stale line than a digest
        # that silently drops everything on an Overseerr that doesn't set
        # mediaAddedAt -- which is the failure this whole path exists to fix.
        when = _parse_ts(media.get("mediaAddedAt")) or _parse_ts(media.get("updatedAt"))
    elif status in _POLL_PENDING:
        event = EVENT_ADDED
        when = _parse_ts(req.get("createdAt"))
    else:
        return None  # UNKNOWN / DELETED -- nothing to say about it
    if when is None or when < cutoff:
        return None

    # Titles cost an HTTP call each, so they're resolved only for survivors.
    tmdb, tvdb = media.get("tmdbId"), media.get("tvdbId")
    if (tmdb, tvdb) not in title_cache:
        title_cache[(tmdb, tvdb)] = overseerr.getMediaTitle(tmdb, tvdb)
    title, media_type = title_cache[(tmdb, tvdb)]
    if not title:
        # Same rule as the *arr webhooks: an untitled row has nothing to print
        # and would dedupe against every other untitled row.
        return None

    is_tv = media_type == "tv"
    requested_by = req.get("requestedBy") or {}
    return {
        "title": title,
        "media_type": media_type,
        # displayName matches what db.linkSeerr() stored as seerr_username,
        # so _matches() can attribute this to a member for their personal DM.
        "requested_by_username": overseerr.displayName(requested_by) if requested_by else None,
        "requested_by_email": requested_by.get("email"),
        "occurred_at": when.isoformat(),
        "source": _POLL_SOURCE,
        "event": event,
        # tmdb for movies, tvdb for tv -- the same namespaces the Radarr and
        # Sonarr webhooks use, so a polled item dedupes against a pushed one.
        "external_id": external_id("tvdb" if is_tv else "tmdb", tvdb if is_tv else tmdb),
    }


def overseerr_events(days=7):
    """The last `days` of library activity as Overseerr sees it, shaped like
    media_events rows. Needs no webhook. Returns [] if Overseerr isn't
    configured, and degrades to [] rather than raising if it's unreachable --
    a dead Overseerr must not cost us the events we did record."""
    if not overseerr.enabled():
        return []
    now = time.monotonic()
    if _poll_cache["days"] == days and now - _poll_cache["at"] < POLL_TTL_SECONDS:
        return list(_poll_cache["events"])

    raw = overseerr.getRequests(max_items=POLL_MAX_REQUESTS)
    if len(raw) >= POLL_MAX_REQUESTS:
        logger.warning(
            f"Overseerr poll hit the {POLL_MAX_REQUESTS}-request cap; older requests were not "
            f"examined and anything among them that became available this week will be missing."
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    title_cache, out = {}, []
    for req in raw:
        try:
            event = _poll_event(req, cutoff, title_cache)
        except Exception as e:
            logger.warning(f"Overseerr poll: skipped request {req.get('id')}: {e}")
            continue
        if event:
            out.append(event)

    _poll_cache.update({"at": now, "days": days, "events": list(out)})
    logger.info(f"Overseerr poll: {len(out)} item(s) in the last {days} days, from {len(raw)} request(s).")
    return out


def collect_events(days=7):
    """Everything this week's digest should consider: what we recorded from
    webhooks and reelay's own adds, plus what Overseerr reports directly.

    Recorded events come first so _dedupe() keeps them as the surviving copy --
    they carry an observed occurred_at, where a polled row's timestamp is
    inferred from Overseerr's current state. The polled duplicate still merges
    its source in, so `sourceCounts` shows both."""
    return db.getRecentMediaEvents(days) + overseerr_events(days)


def _split(events):
    """(available, added) -- both deduped, with anything already watchable
    removed from `added` so an item that was added AND imported in the same
    week is only announced once, under "what's new"."""
    available = _dedupe([e for e in events if e.get("event") != EVENT_ADDED])
    added = _dedupe([e for e in events if e.get("event") == EVENT_ADDED])
    # available first, so a collision resolves in favour of the watchable copy.
    # Both lists come back out of this pass, not just `added`: it's where an
    # item added by one source and imported by another has the two merged into
    # a single entry, and reading `available` from before it would drop that.
    survivors = _dedupe(available + added)
    return ([e for e in survivors if e.get("event") != EVENT_ADDED],
            [e for e in survivors if e.get("event") == EVENT_ADDED])


def _render(events):
    shown = events[:MAX_LINES]
    lines = "\n".join(f"{_icon(e.get('media_type'))} {e.get('title')}" for e in shown)
    extra = len(events) - len(shown)
    if extra > 0:
        lines += "\n" + i18n.t("reelay.Digest.More", count=extra)
    return lines


def _group_text(available, added):
    sections = []
    if available:
        sections.append(i18n.t("reelay.Digest.GroupHeader", count=len(available)) + "\n" + _render(available))
    if added:
        sections.append(i18n.t("reelay.Digest.AddedHeader", count=len(added)) + "\n" + _render(added))
    return "\n\n".join(sections)


def _matches(event, link):
    email = (event.get("requested_by_email") or "").lower()
    username = event.get("requested_by_username") or ""
    link_email = (link.get("seerr_email") or "").lower()
    link_user = link.get("seerr_username") or ""
    return bool((email and email == link_email) or (username and username == link_user))


def _entry(event):
    return {"title": event.get("title"), "sources": sorted(_sources_of(event))}


def weekly_breakdown(events):
    """This week's (deduped) events for the Mini App's "this week" view:
    counts/movies/tv cover what became watchable; `added` lists what's queued
    but not downloaded yet. `other` catches events whose source didn't say
    which media type it was, so a title can never silently vanish from the
    list while still being counted in the digest.

    Each entry is {title, sources} and `sourceCounts` tallies how many items
    each source reported -- the only place an admin can see whether a wired-up
    webhook is actually delivering. An item reported by two sources counts in
    both, so these deliberately don't sum to the item total."""
    available, added = _split(events)
    buckets = {"movie": [], "tv": [], "other": []}
    for e in available:
        buckets.get(e.get("media_type"), buckets["other"]).append(_entry(e))

    source_counts = {}
    for e in available + added:
        for source in _sources_of(e):
            source_counts[source] = source_counts.get(source, 0) + 1

    return {
        "counts": {k: len(v) for k, v in buckets.items()},
        "sourceCounts": dict(sorted(source_counts.items())),
        "movies": buckets["movie"],
        "tv": buckets["tv"],
        "other": buckets["other"],
        "added": [_entry(e) for e in added],
    }


async def send_weekly_digest_to_scope(context, scope, events):
    """Post the group "what's new" into `scope`'s #updates topic and DM each
    linked member the items THEY requested that became available. `events`
    is the shared (not-yet-deduped) media_events list for the whole run --
    callers fetch it once and pass it to every due scope.

    Returns True if anything actually reached Telegram (the group post landed,
    or at least one DM went out). A False here is the difference between a
    quiet week and a misconfigured scope, so callers should not report success
    on it -- see send_weekly_now() in miniapp.py."""
    chat_id = scope["chat_id"]
    available, added = _split(events)
    if not (available or added):
        logger.info(f"Scope {chat_id}: weekly digest skipped, nothing recorded this week.")
        return False
    shim = types.SimpleNamespace(bot=context.bot)

    # Group "what's new" into the scope's #updates topic (plain text --
    # media titles must not be interpreted as Markdown).
    posted = await channels.announce(
        shim, chat_id, channels.CATEGORY_UPDATES, _group_text(available, added), parse_mode=None
    )
    if not posted:
        # Silent until now: an admin who never ran '/routehere updates' saw
        # nothing at all and had no way to tell why.
        logger.warning(
            f"Scope {chat_id}: weekly digest group post not delivered -- no "
            f"'{channels.CATEGORY_UPDATES}' route configured (/routehere updates), or the send failed."
        )

    # Personalized DMs -- 'available' only; there's no point telling someone
    # their request is "ready for you" while it's still downloading.
    dmed = set()
    for m in db.getApprovedMembers(chat_id):
        uid = m["user_id"]
        if uid in dmed:
            continue
        link = db.getSeerrLink(chat_id, uid)
        if not link:
            continue
        mine = _dedupe([e for e in available if _matches(e, link)])
        if not mine:
            continue
        text = i18n.t("reelay.Digest.PersonalHeader", count=len(mine)) + "\n" + _render(mine)
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
            dmed.add(uid)
        except Exception:
            logger.warning(f"Could not DM weekly digest to {uid}.")

    logger.info(
        f"Scope {chat_id}: weekly digest -- {len(available)} available, {len(added)} added, "
        f"group_posted={posted}, dms={len(dmed)}."
    )
    return bool(posted or dmed)


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

    events = collect_events(7)
    if not events:
        logger.info(
            f"Weekly digest: {len(due)} scope(s) due but nothing in the last 7 days -- no recorded "
            f"webhook/reelay events, and Overseerr reported nothing either."
        )
        db.pruneMediaEvents(30)
        return

    for scope in due:
        await send_weekly_digest_to_scope(context, scope, events)
        # Marked regardless of delivery: the due-window is a single hour, so a
        # failed send can't be usefully retried, while an unmarked scope would
        # re-DM every member if the hourly tick fires twice inside that hour.
        db.markWeeklyDigestSent(scope["chat_id"], today_str)

    db.pruneMediaEvents(30)
