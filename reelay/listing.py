from telegram.ext import ConversationHandler
import logging
from . import logger

from .commons import requestChatAccess, checkAllowed, checkId, format_long_list_message
from .config import config
from .translations import i18n
from . import radarr
from . import sonarr

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.all", logLevel, config.get("logToConsole", False))


async def allSeries(update, context):
    if config.get("enableAllowlist") and not checkAllowed(update,"regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END

    if sonarr.config.get("adminRestrictions") and not checkAllowed(update,"admin"):
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=i18n.t("reelay.NotAdmin"),
        )
        return ConversationHandler.END

    if not checkId(update):
        await requestChatAccess(update, context)
        return ConversationHandler.END
    else:
        result = sonarr.allSeries()
        content = format_long_list_message(result)

        if isinstance(content, str):
            await context.bot.send_message(
                chat_id=update.effective_message.chat_id,
                text=content,
            )
        else:
            # print every substring
            for subString in content:
                await context.bot.send_message(
                    chat_id=update.effective_message.chat_id,
                    text=subString,
                )
        return ConversationHandler.END


async def allMovies(update, context):
    if config.get("enableAllowlist") and not checkAllowed(update,"regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END
        
    if radarr.config.get("adminRestrictions") and not checkAllowed(update,"admin"):
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=i18n.t("reelay.NotAdmin"),
        )
        return ConversationHandler.END
    
    if not checkId(update):
        await requestChatAccess(update, context)
        return ConversationHandler.END
    else:
        result = radarr.all_movies()
        content = format_long_list_message(result)

        if isinstance(content, str):
            await context.bot.send_message(
                chat_id=update.effective_message.chat_id,
                text=content,
            )
        else:
            # print every substring
            for subString in content:
                await context.bot.send_message(
                    chat_id=update.effective_message.chat_id,
                    text=subString,
                )
        return ConversationHandler.END