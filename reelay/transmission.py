import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from .commons import requestChatAccess, checkAllowed, checkId, guardCallbackOwner, stampCallbackOwner
from .config import config
from .translations import i18n
import logging
from . import logger

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.transmission", logLevel, config.get("logToConsole", False))

config = config["transmission"]

TSL_LIMIT, TSL_NORMAL = range(2)


async def transmission(update, context):
    if config.get("enableAllowlist") and not checkAllowed(update,"regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END
    
    if not config["enable"]:
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=i18n.t("reelay.Transmission.NotEnabled"),
        )
        return ConversationHandler.END

    if not checkId(update):
        await requestChatAccess(update, context)
        return ConversationHandler.END

    if config["onlyAdmin"] and not checkAllowed(update, "admin"):
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=i18n.t("reelay.NotAdmin"),
        )
        return TSL_NORMAL

    keyboard = [[
        InlineKeyboardButton(
            '\U0001F40C '+i18n.t("reelay.Transmission.TSL"),
            callback_data=TSL_LIMIT
        ),
        InlineKeyboardButton(
            '\U0001F406 '+i18n.t("reelay.Transmission.Normal"),
            callback_data=TSL_NORMAL
        ),
    ]]
    markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        i18n.t("reelay.Transmission.Speed"), reply_markup=markup
    )
    stampCallbackOwner(update, context, msg.message_id)
    return TSL_NORMAL


async def changeSpeedTransmission(update, context):
    if not await guardCallbackOwner(update, context):
        return

    if not checkId(update):
        return ConversationHandler.END

    choice = update.callback_query.data
    command = f"transmission-remote {config['host']}"
    if config["authentication"]:
        command += (
            " --auth "
            + config["username"]
            + ":"
            + config["password"]
        )
    
    message = None
    if choice == TSL_NORMAL:
        command += ' --no-alt-speed'
        message = i18n.t("reelay.Transmission.ChangedToNormal")
    elif choice == TSL_LIMIT:
        command += ' --alt-speed'
        message=i18n.t("reelay.Transmission.ChangedToTSL"),
    
    os.system(command)

    await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=message,
        )
    return ConversationHandler.END
