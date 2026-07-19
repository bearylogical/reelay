import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from .commons import requestChatAccess, checkAllowed, checkId, generateApiQuery, guardCallbackOwner, stampCallbackOwner
from .config import config
from .translations import i18n
import logging
from . import logger

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.sabnzbd", logLevel, config.get("logToConsole", False))

config = config["sabnzbd"]

SABNZBD_SPEED_LIMIT_25, SABNZBD_SPEED_LIMIT_50, SABNZBD_SPEED_LIMIT_100 = range(3)


async def sabnzbd(update, context):
    if config.get("enableAllowlist") and not checkAllowed(update,"regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END
        
    if not config["enable"]:
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=i18n.t("reelay.Sabnzbd.NotEnabled"),
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
        return SABNZBD_SPEED_LIMIT_100

    keyboard = [[
        InlineKeyboardButton(
            '\U0001F40C ' + i18n.t("reelay.Sabnzbd.Limit25"),
            callback_data=SABNZBD_SPEED_LIMIT_25
        ),
        InlineKeyboardButton(
            '\U0001F40E ' + i18n.t("reelay.Sabnzbd.Limit50"),
            callback_data=SABNZBD_SPEED_LIMIT_50
        ),
        InlineKeyboardButton(
            '\U0001F406 ' + i18n.t("reelay.Sabnzbd.Limit100"),
            callback_data=SABNZBD_SPEED_LIMIT_100
        ),
    ]]
    markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        i18n.t("reelay.Sabnzbd.Speed"), reply_markup=markup
    )
    stampCallbackOwner(update, context, msg.message_id)
    return SABNZBD_SPEED_LIMIT_100


async def changeSpeedSabnzbd(update, context):
    if not await guardCallbackOwner(update, context):
        return

    if not checkId(update):
        return ConversationHandler.END

    choice = update.callback_query.data

    url = generateApiQuery("sabnzbd", "",
                           {'output': 'json', 'mode': 'config', 'name': 'speedlimit', 'value': choice})

    try:
        req = requests.get(url)
        status = req.status_code
    except Exception as e:
        logger.warning(f"SABnzbd speedlimit change failed: {e}")
        status = None

    message = None
    if status == 200:
        if choice == SABNZBD_SPEED_LIMIT_100:
            message = i18n.t("reelay.Sabnzbd.ChangedTo100")
        elif choice == SABNZBD_SPEED_LIMIT_50:
            message = i18n.t("reelay.Sabnzbd.ChangedTo50")
        elif choice == SABNZBD_SPEED_LIMIT_25:
            message = i18n.t("reelay.Sabnzbd.ChangedTo25")

    else:
        if status is not None:
            logger.warning(f"SABnzbd speedlimit change failed: status={status}")
        message = i18n.t("reelay.Sabnzbd.Error")

    await context.bot.send_message(
        chat_id=update.effective_message.chat_id,
        text=message,
    )

    return ConversationHandler.END
