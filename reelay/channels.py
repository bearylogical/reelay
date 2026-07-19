import logging

from telegram.constants import ParseMode

from . import db
from . import logger
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.channels", logLevel, config.get("logToConsole", False))

# Categories of bot output that can be pinned to a specific channel/topic.
# 'requests' = a member asked for something (bot-initiated); 'updates' =
# Overseerr status events (approved/available/failed, via the webhook).
CATEGORY_REQUESTS = "requests"
CATEGORY_UPDATES = "updates"
ROUTE_CATEGORIES = {CATEGORY_REQUESTS, CATEGORY_UPDATES}


def _isScopeAdmin(scope_chat_id, user_id):
    m = db.getMembership(scope_chat_id, user_id)
    return bool(m and m["role"] == "admin" and m["status"] == "approved")


def _requireGroupScope(update):
    """Returns (scope, error_key). Exactly one is non-None."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return None, "reelay.Channels.GroupOnly"
    scope = db.getScope(chat.id)
    if scope is None:
        return None, "reelay.Channels.NoScope"
    return scope, None


async def routehere(update, context):
    """/routehere <category> - pin a category's output to the current topic.
    Run it inside the forum topic you want (e.g. #requests)."""
    scope, err = _requireGroupScope(update)
    if err:
        await update.message.reply_text(i18n.t(err))
        return
    if not _isScopeAdmin(update.effective_chat.id, update.effective_user.id):
        await update.message.reply_text(i18n.t("reelay.NotAdmin"))
        return

    cats = ", ".join(sorted(ROUTE_CATEGORIES))
    if not context.args:
        await update.message.reply_text(i18n.t("reelay.Channels.RouteUsage", categories=cats))
        return
    category = context.args[0].strip().lower()
    if category not in ROUTE_CATEGORIES:
        await update.message.reply_text(i18n.t("reelay.Channels.BadCategory", categories=cats))
        return

    thread_id = update.effective_message.message_thread_id
    db.setChannelRoute(update.effective_chat.id, category, update.effective_chat.id, thread_id)
    where = i18n.t("reelay.Channels.ThisTopic") if thread_id else i18n.t("reelay.Channels.ThisChat")
    await update.message.reply_text(i18n.t("reelay.Channels.RouteSet", category=category, where=where))


async def routes(update, context):
    """/routes - show the current category -> channel map for this group."""
    scope, err = _requireGroupScope(update)
    if err:
        await update.message.reply_text(i18n.t(err))
        return

    rs = db.getChannelRoutes(update.effective_chat.id)
    if not rs:
        await update.message.reply_text(i18n.t("reelay.Channels.NoRoutes"))
        return
    lines = []
    for r in rs:
        loc = i18n.t("reelay.Channels.LocThread", thread=r["dest_thread_id"]) if r["dest_thread_id"] \
            else i18n.t("reelay.Channels.LocMain")
        lines.append(f"• {r['category']} → {loc}")
    await update.message.reply_text(i18n.t("reelay.Channels.RoutesList", routes="\n".join(lines)))


async def unroute(update, context):
    """/unroute <category> - clear a category's channel routing."""
    scope, err = _requireGroupScope(update)
    if err:
        await update.message.reply_text(i18n.t(err))
        return
    if not _isScopeAdmin(update.effective_chat.id, update.effective_user.id):
        await update.message.reply_text(i18n.t("reelay.NotAdmin"))
        return
    if not context.args:
        await update.message.reply_text(i18n.t("reelay.Channels.RouteUsage", categories=", ".join(sorted(ROUTE_CATEGORIES))))
        return
    category = context.args[0].strip().lower()
    if db.deleteChannelRoute(update.effective_chat.id, category):
        await update.message.reply_text(i18n.t("reelay.Channels.RouteRemoved", category=category))
    else:
        await update.message.reply_text(i18n.t("reelay.Channels.NoSuchRoute", category=category))


async def announce(context, scope_chat_id, category, text, from_chat_id=None, from_thread_id=None, parse_mode=ParseMode.MARKDOWN):
    """Post `text` into the channel configured for `category` in this scope.
    No-ops (returns False) when no route is set, or when the action already
    happened in the destination channel (avoids duplicate posts). Pass
    parse_mode=None for arbitrary text (e.g. media titles) that mustn't be
    parsed as Markdown."""
    if scope_chat_id is None:
        return False
    route = db.getChannelRoute(scope_chat_id, category)
    if not route:
        return False

    from_thread = str(from_thread_id) if from_thread_id is not None else None
    if from_chat_id is not None and str(from_chat_id) == route["dest_chat_id"] and from_thread == route["dest_thread_id"]:
        return False  # already in the destination topic; the inline reply covers it

    kwargs = {"chat_id": int(route["dest_chat_id"]), "text": text, "parse_mode": parse_mode}
    if route["dest_thread_id"]:
        kwargs["message_thread_id"] = int(route["dest_thread_id"])
    try:
        await context.bot.send_message(**kwargs)
        return True
    except Exception:
        logger.warning(f"Could not announce '{category}' to route {route['dest_chat_id']}/{route['dest_thread_id']}.")
        return False
