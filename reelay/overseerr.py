import logging
import requests

from . import logger
from .config import config

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.overseerr", logLevel, config.get("logToConsole", False))


def enabled():
    return bool(config.get("overseerr", {}).get("enable") and config["overseerr"].get("url"))


def _base():
    return config["overseerr"]["url"].rstrip("/") + "/api/v1"


def _headers():
    return {"X-Api-Key": config["overseerr"]["apikey"]}


def getRequests(filter=None, requested_by=None, take=50, max_items=None):
    """Raw Overseerr requests, across pages. `filter` is one of
    available/pending/processing/... ; `requested_by` restricts to a single
    Overseerr user id (how we scope a member to their own requests)."""
    if not enabled():
        return []
    results = []
    skip = 0
    while True:
        params = {"take": take, "skip": skip}
        if filter:
            params["filter"] = filter
        if requested_by is not None:
            params["requestedBy"] = requested_by
        try:
            resp = requests.get(f"{_base()}/request", headers=_headers(), params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"Overseerr getRequests failed: {e}")
            return results
        page = data.get("results", [])
        results.extend(page)
        if len(page) < take or (max_items is not None and len(results) >= max_items):
            break
        skip += take
    return results[:max_items] if max_items is not None else results


def getAvailableRequests():
    """All requests Overseerr currently reports as available, across pages."""
    return getRequests(filter="available")


def getRequestCount():
    """Overseerr's request-count summary: {total, movie, tv, pending,
    approved, declined, processing, available}. Empty dict on failure."""
    if not enabled():
        return {}
    try:
        resp = requests.get(f"{_base()}/request/count", headers=_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Overseerr getRequestCount failed: {e}")
        return {}


def summarizeRequests(raw_requests, title_cache=None):
    """Turn raw Overseerr request objects into display-ready dicts (resolving
    each media's title, with an optional shared cache to dedupe lookups)."""
    if title_cache is None:
        title_cache = {}
    out = []
    for req in raw_requests:
        media = req.get("media") or {}
        tmdb = media.get("tmdbId")
        tvdb = media.get("tvdbId")
        key = (tmdb, tvdb)
        if key not in title_cache:
            title_cache[key] = getMediaTitle(tmdb, tvdb)
        title, media_type = title_cache[key]
        req_status = req.get("status")  # 1 pending, 2 approved, 3 declined
        out.append({
            "id": req.get("id"),
            "title": title or f"tmdb {tmdb}",
            "mediaType": media_type,
            "mediaStatus": media.get("status"),
            "statusLabel": "❌ Declined" if req_status == 3 else statusBadge(media.get("status")),
            "requestedById": (req.get("requestedBy") or {}).get("id"),
            "createdAt": req.get("createdAt"),
        })
    return out


def getWatchedUserIds(media_id):
    """Overseerr user ids that have watched this media, or None if watch data
    could not be determined (e.g. no Tautulli configured in Overseerr)."""
    try:
        resp = requests.get(f"{_base()}/media/{media_id}/watch_data", headers=_headers(), timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Overseerr getWatchedUserIds failed for media {media_id}: {e}")
        return None

    sd = data.get("data") or {}
    sd4k = data.get("data4k") or {}
    users = sd.get("users") or []
    users4k = sd4k.get("users") or []
    if not users and not users4k:
        return None
    return {u["id"] for u in users} | {u["id"] for u in users4k}


# MediaInfo.status: 1 UNKNOWN, 2 PENDING, 3 PROCESSING, 4 PARTIALLY_AVAILABLE,
# 5 AVAILABLE, 6 DELETED. Rendered as a short badge in the browse flow.
_STATUS_BADGES = {
    2: "⏳ Requested",
    3: "⏳ Processing",
    4: "🟡 Partially available",
    5: "✅ Available",
}


def statusBadge(status):
    return _STATUS_BADGES.get(status, "➕ Not requested")


def _tmdbPoster(poster_path):
    return f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None


def search(query, media_type):
    """Search Overseerr for `media_type` ('movie' or 'tv'). Returns a list of
    normalized dicts carrying the tmdbId as `id`, plus title/year/poster and
    the current availability `status`. Empty list on failure or no matches."""
    if not enabled():
        return []
    try:
        resp = requests.get(
            f"{_base()}/search",
            headers=_headers(),
            params={"query": query, "page": 1},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Overseerr search failed for '{query}': {e}")
        return []

    results = []
    for item in data.get("results", []):
        if item.get("mediaType") != media_type:
            continue
        if media_type == "movie":
            title = item.get("title")
            date = item.get("releaseDate") or ""
        else:
            title = item.get("name")
            date = item.get("firstAirDate") or ""
        media_info = item.get("mediaInfo") or {}
        results.append({
            "id": item.get("id"),  # tmdbId
            "mediaType": media_type,
            "title": title,
            "year": date[:4] if date else "",
            "poster": _tmdbPoster(item.get("posterPath")),
            "status": media_info.get("status"),
        })
    return results


def _displayName(user):
    """Overseerr computes displayName = username || plexUsername || email.
    Replicate that fallback here since the API doesn't always return it."""
    return (
        user.get("displayName")
        or user.get("username")
        or user.get("plexUsername")
        or user.get("email")
        or f"user {user.get('id')}"
    )


def getUsers():
    """All Overseerr/Jellyseerr users (id, display name, plexUsername, email),
    across pages. Plex-backed users carry a plexUsername -- picking one of
    these is what establishes the Plex linkage for a Telegram user."""
    if not enabled():
        return []
    results = []
    skip = 0
    take = 50
    while True:
        try:
            resp = requests.get(
                f"{_base()}/user",
                headers=_headers(),
                params={"take": take, "skip": skip, "sort": "displayname"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"Overseerr getUsers failed: {e}")
            return results
        page = data.get("results", [])
        for u in page:
            results.append({
                "id": u.get("id"),
                "displayName": _displayName(u),
                "plexUsername": u.get("plexUsername"),
                "email": u.get("email"),
                "userType": u.get("userType"),
            })
        if len(page) < take:
            break
        skip += take
    return results


def createRequest(media_type, media_id, requested_by_seerr_id=None, is4k=False, seasons=None):
    """Create an Overseerr request. media_type is 'movie' or 'tv'; media_id is
    the tmdbId. requested_by_seerr_id attributes the request to a specific
    Overseerr user (so per-user visibility and watch tracking work). Returns
    the created request dict on success, or None on failure."""
    if not enabled():
        return None
    payload = {"mediaType": media_type, "mediaId": int(media_id), "is4k": bool(is4k)}
    if requested_by_seerr_id is not None:
        payload["userId"] = int(requested_by_seerr_id)
    if media_type == "tv" and seasons is not None:
        payload["seasons"] = seasons  # list of season numbers, or "all"
    try:
        resp = requests.post(f"{_base()}/request", headers=_headers(), json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Overseerr createRequest failed for {media_type}/{media_id}: {e}")
        return None


def getMediaTitle(tmdb_id, tvdb_id):
    """Best-effort display title for a request's media. movie vs tv is
    inferred from whether tvdbId is present, since Overseerr's MediaInfo
    doesn't expose an explicit mediaType field."""
    is_tv = tvdb_id is not None
    endpoint = "tv" if is_tv else "movie"
    try:
        resp = requests.get(f"{_base()}/{endpoint}/{tmdb_id}", headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Overseerr getMediaTitle failed for {endpoint}/{tmdb_id}: {e}")
        return None, "tv" if is_tv else "movie"
    title = data.get("name") if is_tv else data.get("title")
    return title, ("tv" if is_tv else "movie")
