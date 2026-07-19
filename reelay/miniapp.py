"""Telegram Mini App server for Reelay.

A small aiohttp app, started on the bot's own asyncio loop (via post_init),
serving a single-page dashboard and a JSON API. Authentication is Telegram's
signed initData (HMAC-SHA256, verified against the bot token) -- no separate
login. All data is role-filtered server-side against the caller's per-scope
membership; the client never receives anything it isn't allowed to see.
"""

import hashlib
import hmac
import json as jsonlib
import logging
import time
import types
import urllib.parse
from datetime import datetime

from aiohttp import web

from . import channels
from . import db
from . import digest
from . import logger
from . import overseerr
from . import plex
from . import radarr
from . import sonarr
from . import webhooks
from .config import config
from .definitions import MINIAPP_HTML_PATH
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.miniapp", logLevel, config.get("logToConsole", False))

DASHBOARD_REQUEST_LIMIT = 20  # bound title-resolution latency on load

# Plex PIN flow: pin_id -> {user_id, scope_chat_id, created_at}. Single aiohttp
# process, so an in-memory dict is enough -- pins live minutes, not restarts.
_pending_pins = {}
PLEX_PIN_TTL = 600


def enabled():
    return bool(config.get("miniapp", {}).get("enable") and config["miniapp"].get("url"))


# --- Telegram initData verification (WebApp auth) -----------------------------

def verify_telegram_init_data(init_data_raw, bot_token):
    """Verify the HMAC-SHA256 signature Telegram puts on WebApp initData."""
    try:
        params = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True))
        received_hash = params.pop("hash", "")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, received_hash)
    except Exception:
        logger.warning("initData HMAC verification error", exc_info=True)
        return False


def parse_user(init_data_raw):
    parsed = dict(urllib.parse.parse_qsl(init_data_raw))
    try:
        u = jsonlib.loads(parsed.get("user", "{}"))
        return u if u.get("id") else None
    except (ValueError, TypeError):
        return None


def _resolve_scope_for_user(user_id):
    """The scope this user acts in: their active scope if set and valid, else
    their most-recently-approved membership. Mirrors the DM branch of
    commons.resolveScope without needing a Telegram update object."""
    memberships = db.getApprovedMemberships(user_id)
    if not memberships:
        return None, None
    active = db.getActiveScope(user_id)
    chosen = active if active and any(m["scope_chat_id"] == active for m in memberships) \
        else memberships[0]["scope_chat_id"]
    return db.getScope(chosen), db.getMembership(chosen, user_id)


def _authed(request):
    """Returns (user_id, scope, membership, tg_user). Raises 401/403 otherwise."""
    raw = request.headers.get("X-Telegram-Init-Data") or request.query.get("initData")
    if not raw:
        raise web.HTTPUnauthorized(text="missing initData")
    if not verify_telegram_init_data(raw, config["telegram"]["token"]):
        raise web.HTTPUnauthorized(text="invalid initData signature")
    tg_user = parse_user(raw)
    if not tg_user:
        raise web.HTTPUnauthorized(text="no user in initData")
    scope, membership = _resolve_scope_for_user(tg_user["id"])
    if not membership or membership["status"] != "approved":
        raise web.HTTPForbidden(text="not an approved member")
    return str(tg_user["id"]), scope, membership, tg_user


# --- Endpoints ----------------------------------------------------------------

async def index(request):
    return web.FileResponse(MINIAPP_HTML_PATH)


async def bootstrap(request):
    user_id, scope, membership, tg_user = _authed(request)
    role = membership["role"]
    link = db.getSeerrLink(scope["chat_id"], user_id)

    counts, reqs = {}, []
    if overseerr.enabled():
        counts = overseerr.getRequestCount()
        title_cache = {}
        if role in ("editor", "admin"):
            raw = overseerr.getRequests(max_items=DASHBOARD_REQUEST_LIMIT)
        elif link:
            raw = overseerr.getRequests(requested_by=link["seerr_user_id"], max_items=DASHBOARD_REQUEST_LIMIT)
        else:
            raw = []
        reqs = overseerr.summarizeRequests(raw, title_cache)
        my_seerr = link["seerr_user_id"] if link else None
        for r in reqs:
            r["mine"] = r["requestedById"] == my_seerr

    return web.json_response({
        "role": role,
        "displayName": tg_user.get("username") or tg_user.get("first_name") or user_id,
        "scopeTitle": scope.get("title") or scope["chat_id"],
        "linked": bool(link),
        "seerrUsername": link["seerr_username"] if link else None,
        "overseerrEnabled": overseerr.enabled(),
        "canSeeQueue": role in ("editor", "admin"),
        "counts": counts,
        "requests": reqs,
    })


async def queue(request):
    _, _, membership, _ = _authed(request)
    if membership["role"] not in ("editor", "admin"):
        raise web.HTTPForbidden(text="editor or admin required")
    items = []
    items.extend(radarr.getQueue())
    items.extend(sonarr.getQueue())
    return web.json_response(items)


async def weekly(request):
    """This week's TV/movie breakdown (any approved member) -- same source
    data as the scheduled group digest, see digest.weekly_breakdown()."""
    _authed(request)
    return web.json_response(digest.weekly_breakdown(db.getRecentMediaEvents(7)))


async def send_weekly_now(request):
    """Admin-triggered on-demand post of this week's digest to the scope's
    #updates topic (and personal DMs) -- same content as the scheduled
    weekly_digest_tick, without waiting for the configured day/hour. Marks
    the scope as sent for today so the scheduled tick doesn't double-post
    later the same day."""
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    events = db.getRecentMediaEvents(7)
    if not events:
        return web.json_response({"ok": False, "error": "nothing_this_week"})
    shim = types.SimpleNamespace(bot=request.app["bot"])
    await digest.send_weekly_digest_to_scope(shim, scope, events)
    db.markWeeklyDigestSent(scope["chat_id"], datetime.now().date().isoformat())
    return web.json_response({"ok": True})


async def catalog(request):
    _authed(request)  # any approved member may browse
    q = request.query.get("q", "").strip()
    media_type = request.query.get("type", "movie")
    if media_type not in ("movie", "tv") or not q or not overseerr.enabled():
        return web.json_response([])
    return web.json_response(overseerr.search(q, media_type))


async def submit_request(request):
    user_id, scope, membership, tg_user = _authed(request)
    if not overseerr.enabled():
        return web.json_response({"ok": False, "error": "overseerr_disabled"}, status=400)
    link = db.getSeerrLink(scope["chat_id"], user_id)
    if not link:
        return web.json_response({"ok": False, "error": "not_linked"}, status=409)
    try:
        body = await request.json()
        media_type = body["mediaType"]
        media_id = int(body["mediaId"])
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    if media_type not in ("movie", "tv"):
        return web.json_response({"ok": False, "error": "bad_media_type"}, status=400)

    seasons = "all" if media_type == "tv" else None
    result = overseerr.createRequest(
        media_type, media_id, requested_by_seerr_id=link["seerr_user_id"], is4k=False, seasons=seasons
    )
    if not result:
        return web.json_response({"ok": False, "error": "request_failed"}, status=502)

    # Announce into the scope's request channel (from_chat_id=None -> always
    # posts, since a Mini App request has no originating chat/topic).
    name = tg_user.get("username") or tg_user.get("first_name") or user_id
    title = body.get("title") or f"tmdb {media_id}"
    shim = types.SimpleNamespace(bot=request.app["bot"])
    await channels.announce(
        shim, scope["chat_id"], channels.CATEGORY_REQUESTS,
        i18n.t("reelay.Channels.RequestAnnounce", name=name, title=title),
    )
    return web.json_response({"ok": True})


# --- Plex self-service linking -------------------------------------------------

def _pruneExpiredPins():
    cutoff = time.time() - PLEX_PIN_TTL
    for pin_id in [k for k, v in _pending_pins.items() if v["created_at"] < cutoff]:
        _pending_pins.pop(pin_id, None)


async def plex_start(request):
    user_id, scope, membership, tg_user = _authed(request)
    if not overseerr.enabled():
        return web.json_response({"ok": False, "error": "overseerr_disabled"}, status=400)
    pin = plex.createPin()
    if not pin:
        return web.json_response({"ok": False, "error": "plex_unavailable"}, status=502)
    _pruneExpiredPins()
    _pending_pins[pin["id"]] = {"user_id": user_id, "scope_chat_id": scope["chat_id"], "created_at": time.time()}
    return web.json_response({"authUrl": plex.authUrl(pin["code"]), "pinId": pin["id"]})


async def plex_poll(request):
    user_id, scope, membership, tg_user = _authed(request)
    _pruneExpiredPins()
    try:
        pin_id = int(request.query.get("pinId", ""))
    except ValueError:
        return web.json_response({"status": "expired"})

    pending = _pending_pins.get(pin_id)
    if not pending or pending["user_id"] != user_id or pending["scope_chat_id"] != scope["chat_id"]:
        return web.json_response({"status": "expired"})

    token = plex.pollPin(pin_id)
    if not token:
        return web.json_response({"status": "pending"})

    seerr_user, cookie = overseerr.signInWithPlex(token)
    if not seerr_user:
        return web.json_response({"status": "error", "error": "overseerr_rejected"})

    name = overseerr.displayName(seerr_user)
    db.linkSeerr(
        scope["chat_id"], user_id, int(seerr_user["id"]),
        seerr_username=name, seerr_email=seerr_user.get("email"),
        mode="normal", session_cookie=cookie,
    )
    _pending_pins.pop(pin_id, None)
    return web.json_response({"status": "linked", "displayName": name})


# --- Admin: member management --------------------------------------------------

def _require_admin(membership):
    if membership["role"] != "admin":
        raise web.HTTPForbidden(text="admin required")


async def _notify(request, telegram_user_id, text):
    try:
        await request.app["bot"].send_message(chat_id=telegram_user_id, text=text)
    except Exception:
        logger.warning(f"Could not DM {telegram_user_id} about a membership change.")


async def members(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    rows = db.getMemberships(scope["chat_id"])
    return web.json_response({
        "inviteCode": scope["invite_code"],
        "joinPolicy": scope["join_policy"],
        "allowGroupRequests": db.isFeatureEnabled(scope["chat_id"], db.FEATURE_GROUP_REQUESTS, default=False),
        "weeklyDigest": {
            "enabled": bool(scope["weekly_digest_enabled"]),
            "day": scope["weekly_digest_day"],
            "hour": scope["weekly_digest_hour"],
        },
        "members": [
            {"userId": m["user_id"], "username": m["username"], "role": m["role"], "status": m["status"]}
            for m in rows
        ],
    })


async def approve_member(request):
    user_id, scope, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["user_id"]
    target_membership = db.getMembership(scope["chat_id"], target)
    if not target_membership:
        raise web.HTTPNotFound(text="no such member")
    db.approveMembership(scope["chat_id"], target, approved_by=user_id)
    await _notify(request, target, i18n.t("reelay.Onboarding.JoinApproved", title=scope.get("title") or scope["chat_id"]))
    return web.json_response({"ok": True})


async def deny_member(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["user_id"]
    target_membership = db.getMembership(scope["chat_id"], target)
    if not target_membership:
        raise web.HTTPNotFound(text="no such member")
    db.denyMembership(scope["chat_id"], target)
    display = target_membership.get("username") or target
    await _notify(request, target, i18n.t("reelay.Onboarding.Denied", name=display))
    return web.json_response({"ok": True})


async def update_member_role(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["user_id"]
    target_membership = db.getMembership(scope["chat_id"], target)
    if not target_membership or target_membership["status"] != "approved":
        raise web.HTTPNotFound(text="no such member")
    try:
        body = await request.json()
        role = body["role"]
    except Exception:
        raise web.HTTPBadRequest(text="bad_request")
    if role not in ("member", "editor", "admin"):
        raise web.HTTPBadRequest(text="bad_role")
    if target_membership["role"] == "admin" and role != "admin" \
            and len(db.getApprovedAdmins(scope["chat_id"])) <= 1:
        return web.json_response({"ok": False, "error": "last_admin"})
    db.setMembershipRole(scope["chat_id"], target, role)
    if role != target_membership["role"]:
        display = target_membership.get("username") or target
        await _notify(request, target, i18n.t(
            "reelay.Onboarding.RoleChanged", title=scope.get("title") or scope["chat_id"], role=role,
        ))
    return web.json_response({"ok": True})


async def remove_member(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["user_id"]
    target_membership = db.getMembership(scope["chat_id"], target)
    if not target_membership:
        raise web.HTTPNotFound(text="no such member")
    if target_membership["role"] == "admin" and target_membership["status"] == "approved" \
            and len(db.getApprovedAdmins(scope["chat_id"])) <= 1:
        return web.json_response({"ok": False, "error": "last_admin"})
    db.removeMembership(scope["chat_id"], target)
    if target_membership["status"] == "approved":
        await _notify(request, target, i18n.t("reelay.Onboarding.Removed", title=scope.get("title") or scope["chat_id"]))
    return web.json_response({"ok": True})


async def update_scope(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    try:
        body = await request.json()
    except Exception:
        body = {}
    join_policy = body.get("joinPolicy")
    if join_policy is not None:
        if join_policy not in ("auto", "approval"):
            raise web.HTTPBadRequest(text="bad_join_policy")
        db.setJoinPolicy(scope["chat_id"], join_policy)
    allow_group_requests = body.get("allowGroupRequests")
    if allow_group_requests is not None:
        if not isinstance(allow_group_requests, bool):
            raise web.HTTPBadRequest(text="bad_allow_group_requests")
        db.setFeature(scope["chat_id"], db.FEATURE_GROUP_REQUESTS, allow_group_requests)

    weekly_enabled = body.get("weeklyDigestEnabled")
    weekly_day = body.get("weeklyDigestDay")
    weekly_hour = body.get("weeklyDigestHour")
    if weekly_enabled is not None and not isinstance(weekly_enabled, bool):
        raise web.HTTPBadRequest(text="bad_weekly_digest_enabled")
    if weekly_hour is not None and not isinstance(weekly_hour, int):
        raise web.HTTPBadRequest(text="bad_weekly_digest_hour")
    if weekly_enabled is not None or weekly_day is not None or weekly_hour is not None:
        try:
            db.setWeeklyDigestConfig(scope["chat_id"], enabled=weekly_enabled, day=weekly_day, hour=weekly_hour)
        except ValueError:
            raise web.HTTPBadRequest(text="bad_weekly_digest_config")

    return web.json_response({"ok": True})


async def regenerate_invite(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    updated = db.rotateInviteCode(scope["chat_id"])
    return web.json_response({"inviteCode": updated["invite_code"]})


# --- Settings backup/restore ----------------------------------------------------
#
# Scoped strictly to this group's own Reelay settings (join policy, feature
# toggles, channel routing, weekly digest schedule) -- never the bot-wide
# config.yaml, which holds credentials (Telegram token, Sonarr/Radarr keys)
# that must never be downloadable through the Mini App.

async def export_scope(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    return web.json_response({
        "joinPolicy": scope["join_policy"],
        "features": db.getFeatures(scope["chat_id"]),
        "channelRoutes": [
            {"category": r["category"], "destChatId": r["dest_chat_id"], "destThreadId": r["dest_thread_id"]}
            for r in db.getChannelRoutes(scope["chat_id"])
        ],
        "weeklyDigest": {
            "enabled": bool(scope["weekly_digest_enabled"]),
            "day": scope["weekly_digest_day"],
            "hour": scope["weekly_digest_hour"],
        },
    })


async def import_scope(request):
    _, scope, membership, _ = _authed(request)
    _require_admin(membership)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad_request")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad_request")

    join_policy = body.get("joinPolicy")
    if join_policy is not None and join_policy not in ("auto", "approval"):
        raise web.HTTPBadRequest(text="bad_join_policy")

    features = body.get("features")
    if features is not None and not isinstance(features, dict):
        raise web.HTTPBadRequest(text="bad_features")

    routes = body.get("channelRoutes")
    if routes is not None:
        if not isinstance(routes, list):
            raise web.HTTPBadRequest(text="bad_channel_routes")
        for r in routes:
            if not isinstance(r, dict) or not r.get("category") or not r.get("destChatId"):
                raise web.HTTPBadRequest(text="bad_channel_routes")

    wd = body.get("weeklyDigest")
    if wd is not None and not isinstance(wd, dict):
        raise web.HTTPBadRequest(text="bad_weekly_digest")

    if join_policy is not None:
        db.setJoinPolicy(scope["chat_id"], join_policy)
    if features is not None:
        for feature, value in features.items():
            db.setFeature(scope["chat_id"], feature, bool(value))
    if routes is not None:
        for existing in db.getChannelRoutes(scope["chat_id"]):
            db.deleteChannelRoute(scope["chat_id"], existing["category"])
        for r in routes:
            db.setChannelRoute(scope["chat_id"], r["category"], r["destChatId"], r.get("destThreadId"))
    if wd is not None:
        try:
            db.setWeeklyDigestConfig(
                scope["chat_id"], enabled=wd.get("enabled"), day=wd.get("day"), hour=wd.get("hour"),
            )
        except ValueError:
            raise web.HTTPBadRequest(text="bad_weekly_digest")

    return web.json_response({"ok": True})


# --- Legacy chat access requests -----------------------------------------------
#
# Bot-wide (not per-scope) requests from the old password-gated direct-command
# surface (see commons.requestChatAccess). Any admin of the caller's resolved
# scope may review them -- there's no separate "bot owner" identity, and by
# the time a request exists at least one scope with an approved admin exists
# (see the plan notes on /claim never needing this gate).

async def chat_requests(request):
    _, _, membership, _ = _authed(request)
    _require_admin(membership)
    rows = db.getPendingChatAccessRequests()
    return web.json_response([
        {"chatId": r["chat_id"], "displayName": r["display_name"], "requestedAt": r["requested_at"]}
        for r in rows
    ])


async def approve_chat_request(request):
    user_id, _, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["chat_id"]
    db.approveChatAccess(target, approved_by=user_id)
    await _notify(request, target, i18n.t("reelay.Chatid added"))
    return web.json_response({"ok": True})


async def deny_chat_request(request):
    user_id, _, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["chat_id"]
    db.denyChatAccess(target, denied_by=user_id)
    await _notify(request, target, i18n.t("reelay.ChatAccess.Denied"))
    return web.json_response({"ok": True})


async def open_chats(request):
    _, _, membership, _ = _authed(request)
    _require_admin(membership)
    rows = db.getApprovedChatAccessRequests()
    return web.json_response([
        {"chatId": r["chat_id"], "displayName": r["display_name"], "approvedAt": r["decided_at"]}
        for r in rows
    ])


async def revoke_chat_request(request):
    user_id, _, membership, _ = _authed(request)
    _require_admin(membership)
    target = request.match_info["chat_id"]
    db.revokeChatAccess(target, revoked_by=user_id)
    await _notify(request, target, i18n.t("reelay.ChatAccess.Revoked"))
    return web.json_response({"ok": True})


def build_app(bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/miniapp/", index)
    app.router.add_get("/miniapp", index)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_get("/api/queue", queue)
    app.router.add_get("/api/weekly", weekly)
    app.router.add_post("/api/weekly/send", send_weekly_now)
    app.router.add_get("/api/catalog", catalog)
    app.router.add_post("/api/request", submit_request)
    app.router.add_post("/api/plex/start", plex_start)
    app.router.add_get("/api/plex/poll", plex_poll)
    app.router.add_get("/api/members", members)
    app.router.add_post("/api/members/{user_id}/approve", approve_member)
    app.router.add_post("/api/members/{user_id}/deny", deny_member)
    app.router.add_patch("/api/members/{user_id}", update_member_role)
    app.router.add_delete("/api/members/{user_id}", remove_member)
    app.router.add_patch("/api/scope", update_scope)
    app.router.add_get("/api/scope/export", export_scope)
    app.router.add_post("/api/scope/import", import_scope)
    app.router.add_post("/api/invite/regenerate", regenerate_invite)
    app.router.add_get("/api/chat-requests", chat_requests)
    app.router.add_post("/api/chat-requests/{chat_id}/approve", approve_chat_request)
    app.router.add_post("/api/chat-requests/{chat_id}/deny", deny_chat_request)
    app.router.add_get("/api/open-chats", open_chats)
    app.router.add_post("/api/open-chats/{chat_id}/revoke", revoke_chat_request)
    # Overseerr status events -> the scope's #updates topic.
    app.router.add_post("/overseerr/webhook/{secret}", webhooks.handle_overseerr)
    return app


async def start_server(application):
    """post_init hook: launch the aiohttp server on the bot's event loop.
    Runs when the Mini App or the Overseerr webhook receiver is enabled."""
    if not (enabled() or webhooks.enabled()):
        return
    app = build_app(application.bot)
    runner = web.AppRunner(app)
    await runner.setup()
    host = config["miniapp"].get("listenHost", "0.0.0.0")
    port = int(config["miniapp"].get("listenPort", 8080))
    site = web.TCPSite(runner, host, port)
    await site.start()
    application.bot_data["_miniapp_runner"] = runner
    logger.info(f"Mini App server listening on {host}:{port} (public: {config['miniapp'].get('url')})")


async def stop_server(application):
    runner = application.bot_data.get("_miniapp_runner")
    if runner:
        await runner.cleanup()
