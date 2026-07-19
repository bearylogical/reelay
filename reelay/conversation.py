"""Shared conversation helpers used by both the add and delete flows.

Lives in its own module so `add`, `delete` and `bot` can all import these
without importing each other -- this is what breaks the add<->delete import
cycle the original codebase had.
"""

import logging

from telegram.ext import ConversationHandler

from . import logger
from . import radarr
from . import sonarr
from .commons import checkId, checkAllowed, guardCallbackOwner, forgetCallbackOwner, requestChatAccess
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.conversation", logLevel, config.get("logToConsole", False))

# Conversation states, shared across the add and delete flows.
SERIE_MOVIE_AUTHENTICATED, READ_CHOICE, GIVE_OPTION, GIVE_PATHS, TSL_NORMAL, GIVE_QUALITY_PROFILES, SELECT_SEASONS = range(7)
SERIE_MOVIE_DELETE, READ_DELETE_CHOICE = 0, 1


def getService(context):
    if context.user_data.get("choice") == i18n.t("reelay.Series"):
        return sonarr
    elif context.user_data.get("choice") == i18n.t("reelay.Movie"):
        return radarr
    else:
        raise ValueError(
            f"Cannot determine service based on unknown or missing choice: {context.user_data.get('choice')}."
        )


def clearUserData(context):
    logger.debug("Removing choice, title, position, paths, and output from context.user_data...")
    for msgKey in ["update_msg", "title_update_msg", "photo_update_msg"]:
        if msgKey in context.user_data:
            forgetCallbackOwner(context, context.user_data[msgKey])
    for x in [
        x for x in ["choice", "title", "position", "output", "paths", "path",
                    "qualityProfiles", "qualityProfile", "update_msg", "title_update_msg",
                    "photo_update_msg", "selectedSeasons", "seasons"]
        if x in context.user_data.keys()
    ]:
        context.user_data.pop(x)


async def stop(update, context):
    if not await guardCallbackOwner(update, context):
        return
    if config.get("enableAllowlist") and not checkAllowed(update, "regular"):
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END
    if not checkId(update):
        await requestChatAccess(update, context)
        return ConversationHandler.END
    if not checkAllowed(update, "admin") and config.get("adminNotifyId") is not None:
        adminNotifyId = config.get("adminNotifyId")
        await context.bot.send_message(
            chat_id=adminNotifyId,
            text=i18n.t("reelay.Notifications.Stop",
                        first_name=update.effective_message.chat.first_name,
                        chat_id=update.effective_message.chat.id),
        )
    clearUserData(context)
    await context.bot.send_message(
        chat_id=update.effective_message.chat_id, text=i18n.t("reelay.End")
    )
    return ConversationHandler.END
