import logging
import math
import os
import time
from . import logger
from .config import config
from .definitions import ADMIN_PATH, ALLOWLIST_PATH
from .translations import i18n
from . import db

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.commons", logLevel, config.get("logToConsole", False))


def generateServerAddr(app):
    try:
        if config[app]["server"]["ssl"]:
            http = "https://"
        else:
            http = "http://"
        try:
            addr = config[app]["server"]["addr"]
            port = config[app]["server"]["port"]
            path = config[app]["server"]["path"]
            return http + addr + ":" + str(port) + path
        except Exception:
            logger.warning("No ip or port defined.")
    except Exception as e:
        logger.warning(f"Generate of serveraddress failed: {e}.")


def cleanUrl(text):
    url = text.replace(" ", "%20")
    return url


def generateApiQuery(app, endpoint, parameters={}):
    try:
        apikey = config[app]["auth"]["apikey"]
        url = (
            generateServerAddr(app) + "api/v3/" + str(endpoint) + "?apikey=" + str(apikey)
        )
        # If parameters exist iterate through dict and add parameters to URL.
        if parameters:
            for key, value in parameters.items():
                url += "&" + key + "=" + value
        return cleanUrl(url)  # Clean URL (validate) and return as string
    except Exception as e:
        logger.warning(f"Generate of APIQUERY failed: {e}.")


# Check if this chat is authorized for the legacy direct-command surface
# (add/delete/list/transmission/sabnzbd). Backed by chat_access_requests --
# see requestChatAccess() below for how a chat gets there.
def checkId(update):
    return db.isChatAuthorized(update.effective_message.chat_id)


def _chatDisplayName(update):
    chat = update.effective_chat
    if chat.username:
        return str(chat.username)
    if chat.title:
        return str(chat.title)
    if chat.first_name or chat.last_name:
        return " ".join(filter(None, [chat.first_name, chat.last_name]))
    return None


async def requestChatAccess(update, context):
    """Called whenever an unauthorized chat hits a legacy-gated command (or
    explicitly runs /auth). Puts the chat into a pending chat_access_requests
    row and notifies scope admins to review it from the Mini App -- there's no
    password anymore, so nothing is ever granted synchronously here."""
    if config.get("enableAllowlist") and not checkAllowed(update, "regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return

    chatid = update.effective_message.chat_id
    if db.isChatAuthorized(chatid):
        await context.bot.send_message(
            chat_id=chatid,
            text=i18n.t("reelay.Chatid already allowed"),
        )
        return

    displayName = _chatDisplayName(update)
    if db.requestChatAccess(chatid, displayName):
        await _notifyAdminsOfChatAccessRequest(context, chatid, displayName)
        await context.bot.send_message(
            chat_id=chatid, text=i18n.t("reelay.ChatAccess.Requested"),
        )
    else:
        await context.bot.send_message(
            chat_id=chatid, text=i18n.t("reelay.ChatAccess.StillPending"),
        )


async def _notifyAdminsOfChatAccessRequest(context, chat_id, display_name):
    """DM every approved admin of every active scope (deduped) that a new
    chat wants access -- they review/approve it from the Mini App's Members
    tab, not from this DM."""
    notified = set()
    for scope in db.getActiveScopes():
        for admin in db.getApprovedAdmins(scope["chat_id"]):
            userId = admin["user_id"]
            if userId in notified:
                continue
            notified.add(userId)
            try:
                await context.bot.send_message(
                    chat_id=userId,
                    text=i18n.t("reelay.ChatAccess.AdminNotify", name=display_name or chat_id, chat_id=chat_id),
                )
            except Exception:
                logger.warning(f"Could not DM admin {userId} about a pending chat access request.")


# Check if user is an admin or an allowed user
def checkAllowed(update, mode):
    if mode == "admin": 
        path = ADMIN_PATH
    else: 
        path = ALLOWLIST_PATH
    admin = False
    if not os.path.exists(path):
        return False
    user = update.effective_user
    with open(path, "r") as file:
        for line in file:
            chatId = line.strip("\n").split(" - ")[0]
            if chatId == str(user["username"]) or chatId == str(user["id"]):
                admin = True
        file.close()
        if admin:
            return True
        else:
            return False


def format_bytes(num, suffix='B'):
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def format_long_list_message(list):
    string = ""
    for item in list:
        string += "• " \
                  + item["title"] \
                  + " (" \
                  + str(item["year"]) \
                  + ")" \
                  + "\n" \
                  + "        status: " \
                  + item["status"] \
                  + "\n" \
                  + "        monitored: " \
                  + str(item["monitored"]).lower() \
                  + "\n"

    # max length of a message is 4096 chars
    if len(string) <= 4096:
        return string
    # split string if longer then 4096 chars
    else:
        neededSplits = math.ceil(len(string) / 4096)
        positionNewLine = []
        index = 0
        while index < len(string):  # Get positions of newline, so that the split will happen after a newline
            i = string.find("\n", index)
            if i == -1:
                return positionNewLine
            positionNewLine.append(i)
            index += 1

        # split string at newline closest to maxlength
        stringParts = []
        lastSplit = timesSplit = 0
        i = 4096
        while i > 0 and len(string) > 4096:
            if timesSplit < neededSplits:
                if i + lastSplit in positionNewLine:
                    stringParts.append(string[0:i])
                    string = string[i + 1:]
                    timesSplit += 1
                    lastSplit = i
                    i = 4096
            i -= 1
        stringParts.append(string)
        return stringParts


def getAuthChats():
    return db.getApprovedChatIds()


# --- Group-mode: inline keyboard owner-locking -------------------------------
#
# Telegram lets ANY member of a group tap ANY inline button on ANY message.
# Reelay's conversation flows cache state in context.user_data, which is
# per-Telegram-user, so a different user tapping someone else's keyboard
# won't resolve the right state. Rather than mutating callback_data (which
# would break several already-anchored `pattern=` regexes elsewhere in the
# codebase), we track message_id -> owner_user_id in bot_data and gate on it.

CALLBACK_OWNER_MAX_AGE = 3600  # seconds


def stampCallbackOwner(update, context, message_id):
    if update.effective_chat.type not in ("group", "supergroup"):
        return  # only needed where more than one user can see the keyboard
    owners = context.bot_data.setdefault("callback_owners", {})
    owners[message_id] = (update.effective_user.id, time.time())
    _pruneCallbackOwners(owners)


def forgetCallbackOwner(context, message_id):
    context.bot_data.get("callback_owners", {}).pop(message_id, None)


def _pruneCallbackOwners(owners):
    cutoff = time.time() - CALLBACK_OWNER_MAX_AGE
    for message_id in [mid for mid, (_, ts) in owners.items() if ts < cutoff]:
        owners.pop(message_id, None)


async def guardCallbackOwner(update, context):
    """Returns True if this callback click is allowed to proceed. If it's a
    group-chat click on a keyboard owned by a different user (or a keyboard
    from before a bot restart, which is untracked), answers with an alert and
    returns False without touching any conversation state."""
    query = update.callback_query
    if query is None or update.effective_chat.type not in ("group", "supergroup"):
        return True
    owners = context.bot_data.get("callback_owners", {})
    owner = owners.get(query.message.message_id)
    if owner is not None and owner[0] == update.effective_user.id:
        return True
    await query.answer(i18n.t("reelay.NotYourRequest"), show_alert=True)
    return False


# --- Group-mode: scope resolution --------------------------------------------
#
# In a group/supergroup the acting scope is unambiguous (the group itself).
# In a DM, a user may belong to more than one approved scope, so resolve
# their "active" one (sticky via /switch) or fall back to their only/most
# recently approved membership.

async def resolveScope(update, context):
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        return db.getScope(chat.id)

    memberships = db.getApprovedMemberships(update.effective_user.id)
    if not memberships:
        return None
    if len(memberships) == 1:
        return db.getScope(memberships[0]["scope_chat_id"])

    active = db.getActiveScope(update.effective_user.id)
    if active and any(m["scope_chat_id"] == active for m in memberships):
        return db.getScope(active)

    chosen = memberships[0]["scope_chat_id"]
    db.setActiveScope(update.effective_user.id, chosen)
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=i18n.t("reelay.SwitchedTo", title=memberships[0]["scope_title"]),
    )
    return db.getScope(chosen)
