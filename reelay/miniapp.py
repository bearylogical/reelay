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
import types
import urllib.parse

from aiohttp import web

from . import channels
from . import db
from . import logger
from . import overseerr
from . import radarr
from . import sonarr
from . import webhooks
from .config import config
from .definitions import MINIAPP_HTML_PATH
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.miniapp", logLevel, config.get("logToConsole", False))

DASHBOARD_REQUEST_LIMIT = 20  # bound title-resolution latency on load


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


def build_app(bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/miniapp/", index)
    app.router.add_get("/miniapp", index)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_get("/api/queue", queue)
    app.router.add_get("/api/catalog", catalog)
    app.router.add_post("/api/request", submit_request)
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
