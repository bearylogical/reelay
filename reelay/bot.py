#!/usr/bin/env python3

import logging
import re

from datetime import datetime, timedelta, time as dtime

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                      Update, WebAppInfo)
import telegram
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (CallbackQueryHandler, ChatMemberHandler, CommandHandler,
                          ConversationHandler, filters, MessageHandler,
                          Application)
from telegram.warnings import PTBUserWarning

from .commons import (checkAllowed, checkId, authentication, format_bytes, getAuthChats,
                      guardCallbackOwner, stampCallbackOwner, forgetCallbackOwner, resolveScope)
from .conversation import (SERIE_MOVIE_AUTHENTICATED, READ_CHOICE, GIVE_OPTION, GIVE_PATHS, TSL_NORMAL, GIVE_QUALITY_PROFILES, SELECT_SEASONS, SERIE_MOVIE_DELETE, READ_DELETE_CHOICE, stop, getService, clearUserData)
from . import db
from . import channels
from . import digest
from . import miniapp
from . import onboarding
from . import overseerr
from . import logger
from . import radarr
from . import sonarr
from . import delete
from . import listing
from .config import checkConfigValues, config, checkConfig
from .translations import i18n
from warnings import filterwarnings

import asyncio

from . import __version__

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay", logLevel, config.get("logToConsole", False))
logger.debug(f"Reelay v{__version__} starting up...")



async def startCheck():
    bot = telegram.Bot(token=config["telegram"]["token"])
    missingConfig = checkConfig()
    wrongValues = checkConfigValues()
    check=True
    if missingConfig: #empty list is False
        check = False
        logger.error(i18n.t("reelay.Missing config", missingKeys=f"{missingConfig}"[1:-1]))
        for chat in getAuthChats():
            await bot.send_message(chat_id=chat, text=i18n.t("reelay.Missing config", missingKeys=f"{missingConfig}"[1:-1]))
    if wrongValues:
        check=False
        logger.error(i18n.t("reelay.Wrong values", wrongValues=f"{wrongValues}"[1:-1]))
        for chat in getAuthChats():
            await bot.send_message(chat_id=chat, text=i18n.t("reelay.Wrong values", wrongValues=f"{wrongValues}"[1:-1]))
    return check


# --- Group mode: auto-register a scope when the bot is added to a group ------

async def onBotChatMemberUpdate(update, context):
    result = update.my_chat_member
    chat = result.chat
    if chat.type not in ("group", "supergroup"):
        return

    oldStatus = result.old_chat_member.status
    newStatus = result.new_chat_member.status

    wasIn = oldStatus in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    isIn = newStatus in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

    if isIn and not wasIn:
        scope = db.upsertScope(chat.id, title=chat.title)
        adder = result.from_user
        try:
            adderMember = await context.bot.get_chat_member(chat.id, adder.id)
            adderIsGroupAdmin = adderMember.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
        except Exception:
            adderIsGroupAdmin = False

        if adderIsGroupAdmin:
            db.upsertMembership(chat.id, adder.id, adder.username, role="admin", status="pending")
            db.approveMembership(chat.id, adder.id, approved_by=adder.id, role="admin")
            await context.bot.send_message(
                chat_id=chat.id,
                text=i18n.t("reelay.GroupMode.Activated", name=adder.username or adder.first_name or str(adder.id), code=scope["invite_code"]),
            )
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text=i18n.t("reelay.GroupMode.NeedsClaim"),
            )
    elif wasIn and not isIn:
        db.setScopeActive(chat.id, False)


async def claimAdmin(update, context):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(i18n.t("reelay.GroupMode.ClaimGroupOnly"))
        return

    scope = db.getScope(chat.id)
    if scope is None:
        scope = db.upsertScope(chat.id, title=chat.title)

    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None

    if member is None or member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await update.message.reply_text(i18n.t("reelay.GroupMode.ClaimNotAdmin"))
        return

    db.upsertMembership(chat.id, user.id, user.username, role="admin", status="pending")
    db.approveMembership(chat.id, user.id, approved_by=user.id, role="admin")
    await update.message.reply_text(
        i18n.t("reelay.GroupMode.Activated", name=user.username or user.first_name or str(user.id), code=scope["invite_code"])
    )


async def onMemberChatMemberUpdate(update, context):
    result = update.chat_member
    chat = result.chat
    if chat.type not in ("group", "supergroup"):
        return

    scope = db.getScope(chat.id)
    if scope is None or not scope["is_active"]:
        return

    oldStatus = result.old_chat_member.status
    newStatus = result.new_chat_member.status
    wasIn = oldStatus in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    isIn = newStatus in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    if not isIn or wasIn:
        return

    user = result.new_chat_member.user
    if db.getMembership(chat.id, user.id) is not None:
        return

    await context.bot.send_message(
        chat_id=chat.id,
        text=i18n.t("reelay.GroupMode.WelcomeNewMember", name=user.username or user.first_name or str(user.id), code=scope["invite_code"]),
    )


# --- Group mode: DM scope switching -------------------------------------------

async def switchScope(update, context):
    if update.effective_chat.type != "private":
        await update.message.reply_text(i18n.t("reelay.GroupMode.SwitchDmOnly"))
        return

    memberships = db.getApprovedMemberships(update.effective_user.id)
    if not memberships:
        await update.message.reply_text(i18n.t("reelay.Onboarding.NoMembership"))
        return
    if len(memberships) == 1:
        await update.message.reply_text(i18n.t("reelay.GroupMode.OnlyOneScope", title=memberships[0]["scope_title"]))
        return

    keyboard = [
        [InlineKeyboardButton(m["scope_title"] or m["scope_chat_id"], callback_data=f"switch_scope_{m['scope_chat_id']}")]
        for m in memberships
    ]
    msg = await update.message.reply_text(i18n.t("reelay.GroupMode.SwitchPrompt"), reply_markup=InlineKeyboardMarkup(keyboard))
    stampCallbackOwner(update, context, msg.message_id)


async def handleSwitchScope(update, context):
    if not await guardCallbackOwner(update, context):
        return
    query = update.callback_query
    scopeChatId = query.data[len("switch_scope_"):]
    db.setActiveScope(update.effective_user.id, scopeChatId)
    scope = db.getScope(scopeChatId)
    await query.edit_message_text(i18n.t("reelay.GroupMode.SwitchedTo", title=scope.get("title") or scopeChatId))


# --- Mini App ----------------------------------------------------------------

async def openApp(update, context):
    """/app - open the Mini App dashboard via an inline WebApp button."""
    if not miniapp.enabled():
        await update.message.reply_text(i18n.t("reelay.MiniApp.NotEnabled"))
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(i18n.t("reelay.MiniApp.Open"), web_app=WebAppInfo(url=config["miniapp"]["url"]))
    ]])
    await update.message.reply_text(i18n.t("reelay.MiniApp.Prompt"), reply_markup=keyboard)


async def onStartup(application):
    """post_init: launch the Mini App server and set the persistent menu button."""
    await miniapp.start_server(application)
    if miniapp.enabled():
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text=i18n.t("reelay.MiniApp.MenuButton"),
                    web_app=WebAppInfo(url=config["miniapp"]["url"]),
                )
            )
        except Exception:
            logger.warning("Could not set the Mini App menu button.")


async def onShutdown(application):
    await miniapp.stop_server(application)


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _nextWeekly(day, hour):
    """Next datetime matching `day` (name) at `hour`, local time."""
    target_dow = _WEEKDAYS.get(str(day).lower(), 0)
    now = datetime.now()
    days_ahead = (target_dow - now.weekday()) % 7
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return target


# --- Reminders: onboarding-driven, watched-aware ------------------------------

async def sendReminders(context):
    if not overseerr.enabled():
        return

    for scope in db.getActiveScopes():
        requests = overseerr.getAvailableRequests()
        for req in requests:
            requestedBy = req.get("requestedBy") or {}
            seerrUserId = requestedBy.get("id")
            if seerrUserId is None:
                continue
            link = db.getSeerrLinkByOverseerrUser(scope["chat_id"], seerrUserId)
            if link is None:
                continue

            membership = db.getMembership(scope["chat_id"], link["user_id"])
            if not membership or not membership.get("reminder_threshold_days"):
                continue  # not onboarded yet, or explicitly disabled (0/NULL)

            media = req.get("media") or {}
            mediaId = media.get("id")
            requestId = req.get("id")
            if mediaId is None or requestId is None:
                continue

            state = db.getReminderState(scope["chat_id"], requestId, link["user_id"])
            if state is None:
                title, mediaType = overseerr.getMediaTitle(media.get("tmdbId"), media.get("tvdbId"))
                db.createReminderStatePending(scope["chat_id"], requestId, mediaId, link["user_id"], title=title, media_type=mediaType)
                continue  # first time observed as available; start the clock

            if state["resolved"] != "pending":
                continue

            availableSince = datetime.fromisoformat(state["available_since"])
            daysAvailable = (datetime.now(availableSince.tzinfo) - availableSince).days
            if daysAvailable < membership["reminder_threshold_days"]:
                continue

            watchedUserIds = overseerr.getWatchedUserIds(mediaId)
            if watchedUserIds is None:
                db.markReminderResolved(scope["chat_id"], requestId, link["user_id"], "unknown")
                continue
            if seerrUserId in watchedUserIds:
                db.markReminderResolved(scope["chat_id"], requestId, link["user_id"], "watched")
                continue

            try:
                await context.bot.send_message(
                    chat_id=link["user_id"],
                    text=i18n.t("reelay.Onboarding.ReminderNudge", title=state.get("title") or "it"),
                )
            except Exception:
                logger.warning(f"Could not DM reminder to {link['user_id']}.")
            db.markReminderResolved(scope["chat_id"], requestId, link["user_id"], "reminded", sent=True)


def main():
    filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)

    db.initDb()

    application = Application.builder().token(config["telegram"]["token"]).build()

    join_handler_command = CommandHandler("join", onboarding.join)
    remindme_handler_command = CommandHandler("remindme", onboarding.remindme)
    linkme_handler_command = CommandHandler("linkme", onboarding.linkme)
    requestlink_handler_command = CommandHandler("requestlink", onboarding.requestlink)
    app_handler_command = CommandHandler("app", openApp)
    routehere_handler_command = CommandHandler("routehere", channels.routehere)
    routes_handler_command = CommandHandler("routes", channels.routes)
    unroute_handler_command = CommandHandler("unroute", channels.unroute)
    claim_handler_command = CommandHandler("claim", claimAdmin)
    switch_handler_command = CommandHandler("switch", switchScope)
    switch_handler_callback = CallbackQueryHandler(handleSwitchScope, pattern=r"^switch_scope_")
    join_approval_handler_callback = CallbackQueryHandler(onboarding.handleApproval, pattern=r"^(approve|deny)_join_")
    seerr_link_handler_callback = CallbackQueryHandler(onboarding.handleSeerrLink, pattern=r"^(slink|sskip)_")
    request_link_handler_callback = CallbackQueryHandler(onboarding.handleRequestLink, pattern=r"^reqlink")
    # Low group number so this runs before the add/delete ConversationHandlers'
    # text handlers -- it only acts (and stops propagation) when this user has
    # an unanswered onboarding reminder-threshold question pending.
    reminder_reply_catcher = MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding.catchReminderThresholdReply)

    # Bare-word (no-slash) entrypoint matches are a private-chat convenience
    # only -- in a group, ordinary conversation can accidentally contain
    # these keywords (e.g. someone just saying "delete" or "auth"), which
    # would otherwise fire the handler on every member's behalf and, for
    # /auth specifically, publicly broadcast a "wrong password" reply. The
    # explicit /command form is unambiguous and stays enabled everywhere.
    auth_handler_command = CommandHandler(config["entrypointAuth"], authentication)
    auth_handler_text = MessageHandler(
                            filters.ChatType.PRIVATE & filters.Regex(
                                re.compile(r"^" + config["entrypointAuth"] + "$", re.IGNORECASE)
                            ),
                            authentication,
                        )
    allSeries_handler_command = CommandHandler(config["entrypointAllSeries"], listing.allSeries)
    allSeries_handler_text = MessageHandler(
                            filters.ChatType.PRIVATE & filters.Regex(
                                re.compile(r"^" + config["entrypointAllSeries"] + "$", re.IGNORECASE)
                            ),
                            listing.allSeries,
                        )

    allMovies_handler_command = CommandHandler(config["entrypointAllMovies"], listing.allMovies)
    allMovies_handler_text = MessageHandler(
        filters.ChatType.PRIVATE & filters.Regex(
            re.compile(r"^" + config["entrypointAllMovies"] + "$", re.IGNORECASE)
        ),
        listing.allMovies,
    )

    deleteMovieserie_handler = ConversationHandler(
        entry_points=[
            CommandHandler(config["entrypointDelete"], delete.delete),
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(
                    re.compile(r'^' + config["entrypointDelete"] + '$', re.IGNORECASE)
                ),
                delete.delete,
            ),
        ],
        states={
            SERIE_MOVIE_DELETE: [MessageHandler(filters.TEXT, choiceSerieMovie)],
            READ_DELETE_CHOICE: [
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.Movie")}|{i18n.t("reelay.Series")})$'),
                    delete.confirmDelete,
                ),
                CallbackQueryHandler(delete.confirmDelete, pattern=f'^({i18n.t("reelay.Movie")}|{i18n.t("reelay.Series")})$')
            ],
            GIVE_OPTION: [
                CallbackQueryHandler(delete.deleteSerieMovie, pattern=f'({i18n.t("reelay.Delete")})'),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.Delete")})$'),
                    delete.deleteSerieMovie
                ),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.New")})$'),
                    delete
                ),
                CallbackQueryHandler(delete, pattern=f'({i18n.t("reelay.New")})'),
            ],
        },
        fallbacks=[
            CommandHandler("stop", stop),
            MessageHandler(filters.Regex("(?i)^"+i18n.t("reelay.Stop")+"$"), stop),
            CallbackQueryHandler(stop, pattern=f"(?i)^"+i18n.t("reelay.Stop")+"$"),
        ],
    )

    addMovieserie_handler = ConversationHandler(
        entry_points=[
            CommandHandler(config["entrypointAdd"], startSerieMovie),
            CommandHandler(i18n.t("reelay.Movie"), startSerieMovie),
            CommandHandler(i18n.t("reelay.Series"), startSerieMovie),
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(
                    re.compile(r'^' + config["entrypointAdd"] + '$', re.IGNORECASE)
                ),
                startSerieMovie,
            ),
        ],
        states={
            SERIE_MOVIE_AUTHENTICATED: [MessageHandler(filters.TEXT, choiceSerieMovie)],
            READ_CHOICE: [
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.Movie")}|{i18n.t("reelay.Series")})$'),
                    searchSerieMovie,
                ),
                CallbackQueryHandler(searchSerieMovie, pattern=f'^({i18n.t("reelay.Movie")}|{i18n.t("reelay.Series")})$'),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.New")})$'),
                    startSerieMovie
                ),
                CallbackQueryHandler(startSerieMovie, pattern=f'({i18n.t("reelay.New")})'),
            ],
            GIVE_OPTION: [
                CallbackQueryHandler(qualityProfileSerieMovie, pattern=f'({i18n.t("reelay.Select")})'),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.Select")})$'),
                    qualityProfileSerieMovie
                ),
                CallbackQueryHandler(pathSerieMovie, pattern=f'({i18n.t("reelay.Add")})'),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.Add")})$'),
                    pathSerieMovie
                ),
                CallbackQueryHandler(nextOption, pattern=f'({i18n.t("reelay.Next result")})'),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.Next result")})$'),
                    nextOption
                ),
                MessageHandler(
                    filters.Regex(f'^({i18n.t("reelay.New")})$'),
                    startSerieMovie
                ),
                CallbackQueryHandler(startSerieMovie, pattern=f'({i18n.t("reelay.New")})'),
            ],
            GIVE_PATHS: [
                CallbackQueryHandler(qualityProfileSerieMovie, pattern="^(Path: )(.*)$"),
            ],
            GIVE_QUALITY_PROFILES: [
                CallbackQueryHandler(selectSeasons, pattern="^(Quality profile: )(.*)$"),
            ],
            SELECT_SEASONS: [
                CallbackQueryHandler(checkSeasons, pattern="^(Season: )(.*)$"),
            ],
        },
        fallbacks=[
            CommandHandler("stop", stop),
            MessageHandler(filters.Regex("(?i)^"+i18n.t("reelay.Stop")+"$"), stop),
            CallbackQueryHandler(stop, pattern=f"(?i)^"+i18n.t("reelay.Stop")+"$"),
        ],
    )
    if config["transmission"]["enable"]:
        from . import transmission
        changeTransmissionSpeed_handler = ConversationHandler(
            entry_points=[
                CommandHandler(config["entrypointTransmission"], transmission.transmission),
                MessageHandler(
                    filters.ChatType.PRIVATE & filters.Regex(
                        re.compile(
                            r"^" + config["entrypointTransmission"] + "$", re.IGNORECASE
                        )
                    ),
                    transmission.transmission,
                ),
            ],
            states={
                transmission.TSL_NORMAL: [
                    CallbackQueryHandler(transmission.changeSpeedTransmission),
                ]
            },
            fallbacks=[
                CommandHandler("stop", stop),
                MessageHandler(filters.Regex("^(Stop|stop)$"), stop),
            ],
        )
        application.add_handler(changeTransmissionSpeed_handler)

    if config["sabnzbd"]["enable"]:
        from . import sabnzbd
        changeSabznbdSpeed_handler = ConversationHandler(
            entry_points=[
                CommandHandler(config["entrypointSabnzbd"], sabnzbd.sabnzbd),
                MessageHandler(
                    filters.ChatType.PRIVATE & filters.Regex(
                        re.compile(
                            r"^" + config["entrypointSabnzbd"] + "$", re.IGNORECASE
                        )
                    ),
                    sabnzbd.sabnzbd,
                ),
            ],
            states={
                sabnzbd.SABNZBD_SPEED_LIMIT_100: [
                    CallbackQueryHandler(sabnzbd.changeSpeedSabnzbd),
                ]
            },
            fallbacks=[
                CommandHandler("stop", stop),
                MessageHandler(filters.Regex("^(Stop|stop)$"), stop),
            ],
        )
        application.add_handler(changeSabznbdSpeed_handler)

    # group=-1: seen before everything else, so an unanswered onboarding
    # question is captured even if the user is also mid another conversation.
    application.add_handler(reminder_reply_catcher, group=-1)

    application.add_handler(ChatMemberHandler(onBotChatMemberUpdate, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(onMemberChatMemberUpdate, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(claim_handler_command)
    application.add_handler(switch_handler_command)
    application.add_handler(switch_handler_callback)
    application.add_handler(join_handler_command)
    application.add_handler(remindme_handler_command)
    application.add_handler(linkme_handler_command)
    application.add_handler(requestlink_handler_command)
    application.add_handler(app_handler_command)
    application.add_handler(routehere_handler_command)
    application.add_handler(routes_handler_command)
    application.add_handler(unroute_handler_command)
    application.add_handler(join_approval_handler_callback)
    application.add_handler(seerr_link_handler_callback)
    application.add_handler(request_link_handler_callback)

    application.add_handler(auth_handler_command)
    application.add_handler(auth_handler_text)
    application.add_handler(allSeries_handler_command)
    application.add_handler(allSeries_handler_text)
    application.add_handler(allMovies_handler_command)
    application.add_handler(allMovies_handler_text)
    application.add_handler(addMovieserie_handler)
    application.add_handler(deleteMovieserie_handler)

    help_handler_command = CommandHandler(config["entrypointHelp"], help)
    application.add_handler(help_handler_command)

    if overseerr.enabled():
        checkHour = config.get("reminderCheckHour", 9)
        application.job_queue.run_repeating(
            sendReminders,
            interval=timedelta(hours=24),
            first=dtime(hour=checkHour, minute=0),
            name="reminder_check",
        )

    if digest.enabled():
        wd = config.get("weeklyDigest", {})
        application.job_queue.run_repeating(
            digest.send_weekly_digest,
            interval=timedelta(weeks=1),
            first=_nextWeekly(wd.get("day", "monday"), int(wd.get("hour", 9))),
            name="weekly_digest",
        )

    # Launch the Mini App server + menu button once the loop is up, and tear
    # it down on shutdown. No-ops when the Mini App is disabled.
    application.post_init = onStartup
    application.post_shutdown = onShutdown

    logger.info(i18n.t("reelay.Start chatting"))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def startSerieMovie(update : Update, context):
    if not await guardCallbackOwner(update, context):
        return

    if config.get("enableAllowlist") and not checkAllowed(update,"regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END
    
    if not checkId(update):
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id, text=i18n.t("reelay.Authorize")
        )
        return SERIE_MOVIE_AUTHENTICATED

    if update.message is not None:
        reply = update.message.text.lower()
    elif update.callback_query is not None:
        reply = update.callback_query.data.lower()
    else:
        return SERIE_MOVIE_AUTHENTICATED

    if reply[1:] in [
        i18n.t("reelay.Series").lower(),
        i18n.t("reelay.Movie").lower(),
    ]:
        logger.debug(
            f"User issued {reply} command, so setting user_data[choice] accordingly"
        )
        context.user_data.update(
            {
                "choice": i18n.t("reelay.Series")
                if reply[1:] == i18n.t("reelay.Series").lower()
                else i18n.t("reelay.Movie")
            }
        )
    elif reply == i18n.t("reelay.New").lower():
        logger.debug("User issued New command, so clearing user_data")
        clearUserData(context)
    
    await context.bot.send_message(
        chat_id=update.effective_message.chat_id, text='\U0001F3F7 '+i18n.t("reelay.Title")
    )
    if not checkAllowed(update,"admin") and config.get("adminNotifyId") is not None:
        adminNotifyId = config.get("adminNotifyId")
        await context.bot.send_message(
            chat_id=adminNotifyId, text=i18n.t("reelay.Notifications.Start", first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
        )
    
    return SERIE_MOVIE_AUTHENTICATED


async def choiceSerieMovie(update, context):
    if not checkId(update):
        if (
            await authentication(update, context) == "added"
        ):  # To also stop the beginning command
            return ConversationHandler.END
    elif update.message.text.lower() == "/stop".lower() or update.message.text.lower() == "stop".lower():
        return await stop(update, context)
    else:
        if update.message is not None:
            reply = update.message.text
            logger.debug(f"reply is {reply}")
        elif update.callback_query is not None:
            reply = update.callback_query.data
        else:
            return SERIE_MOVIE_AUTHENTICATED

        if reply.lower() not in [
            i18n.t("reelay.Series").lower(),
            i18n.t("reelay.Movie").lower(),
        ]:
            logger.debug(
                f"User entered a title {reply}"
            )
            context.user_data["title"] = reply

        if context.user_data.get("choice") in [
            i18n.t("reelay.Series"),
            i18n.t("reelay.Movie"),
        ]:
            logger.debug(
                f"user_data[choice] is {context.user_data['choice']}, skipping step of selecting movie/series"
            )
            return await searchSerieMovie(update, context)
        else:
            keyboard = [
                [
                    InlineKeyboardButton(
                        '\U0001F3AC '+i18n.t("reelay.Movie"),
                        callback_data=i18n.t("reelay.Movie")
                    ),
                    InlineKeyboardButton(
                        '\U0001F4FA '+i18n.t("reelay.Series"),
                        callback_data=i18n.t("reelay.Series")
                    ),
                ],
                [ InlineKeyboardButton(
                        '\U0001F50D '+i18n.t("reelay.New"),
                        callback_data=i18n.t("reelay.New")
                    ),
                ]
            ]
            markup = InlineKeyboardMarkup(keyboard)
            msg = await update.message.reply_text(i18n.t("reelay.What is this?"), reply_markup=markup)
            context.user_data["update_msg"] = msg.message_id
            stampCallbackOwner(update, context, msg.message_id)
        return READ_CHOICE


async def searchSerieMovie(update, context):
    if not await guardCallbackOwner(update, context):
        return

    title = context.user_data["title"]

    if not context.user_data.get("choice"):
        choice = None
        if update.message is not None:
            choice = update.message.text
        elif update.callback_query is not None:
            choice = update.callback_query.data
        context.user_data["choice"] = choice
    
    choice = context.user_data["choice"]
    
    position = context.user_data["position"] = 0

    # When Overseerr is enabled, browse via its search (richer: carries the
    # tmdbId the request API needs, plus current availability status) and
    # route the eventual add through Overseerr. Otherwise, the classic direct
    # Sonarr/Radarr path.
    if overseerr.enabled():
        media_type = "movie" if choice == i18n.t("reelay.Movie") else "tv"
        output = overseerr.search(title, media_type)
        searchResult = output
    else:
        service = getService(context)
        searchResult = service.search(title)
        output = service.giveTitles(searchResult) if searchResult else []

    if not searchResult:
        await context.bot.send_message(
            chat_id=update.effective_message.chat_id,
            text=i18n.t("reelay.searchresults", count=0),
        )
        clearUserData(context)
        return ConversationHandler.END

    context.user_data["output"] = output
    message=i18n.t("reelay.searchresults", count=len(searchResult))
    message += f"\n\n*{context.user_data['output'][position]['title']} ({context.user_data['output'][position]['year']})*"
    if overseerr.enabled():
        message += "\n" + overseerr.statusBadge(context.user_data['output'][position].get('status'))
    
    if "update_msg" in context.user_data:
        await context.bot.edit_message_text(
            message_id=context.user_data["update_msg"],
            chat_id=update.effective_message.chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        msg = await context.bot.send_message(chat_id=update.effective_message.chat_id, text=message,parse_mode=ParseMode.MARKDOWN,)
        context.user_data["update_msg"] = msg.message_id
    
    try:
        img = await context.bot.sendPhoto(
            chat_id=update.effective_message.chat_id,
            photo=context.user_data["output"][position]["poster"],
        )
    except:
        context.user_data["photo_update_msg"] = None
    else:
        context.user_data["photo_update_msg"] = img.message_id
    
    if len(searchResult) == 1:
        keyboard = [
            [
                InlineKeyboardButton(
                    '\U00002795 '+i18n.t("reelay.Add"),
                    callback_data=i18n.t("reelay.Add")
                ),
            ],[
                InlineKeyboardButton(
                    '\U0001F5D1 '+i18n.t("reelay.New"),
                    callback_data=i18n.t("reelay.New")
                ),
            ],[
                InlineKeyboardButton(
                    '\U0001F6D1 '+i18n.t("reelay.Stop"),
                    callback_data=i18n.t("reelay.Stop")
                ),
            ],
        ]
    else: 
        keyboard = [
            [
                InlineKeyboardButton(
                    '\U00002795 '+i18n.t("reelay.Add"),
                    callback_data=i18n.t("reelay.Add")
                ),
            ],[
                InlineKeyboardButton(
                    '\U000023ED '+i18n.t("reelay.Next result"),
                    callback_data=i18n.t("reelay.Next result")
                ),
            ],[
                InlineKeyboardButton(
                    '\U0001F5D1 '+i18n.t("reelay.New"),
                    callback_data=i18n.t("reelay.New")
                ),
            ],[
                InlineKeyboardButton(
                    '\U0001F6D1 '+i18n.t("reelay.Stop"),
                    callback_data=i18n.t("reelay.Stop")
                ),
            ],
        ]
    markup = InlineKeyboardMarkup(keyboard)

    if choice == i18n.t("reelay.Movie"):
        message=i18n.t("reelay.messages.This", subjectWithArticle=i18n.t("reelay.MovieWithArticle").lower())
    else:
        message=i18n.t("reelay.messages.This", subjectWithArticle=i18n.t("reelay.SeriesWithArticle").lower())
    msg = await context.bot.send_message(
        chat_id=update.effective_message.chat_id, text=message, reply_markup=markup
    )
    context.user_data["title_update_msg"] = context.user_data["update_msg"]
    context.user_data["update_msg"] = msg.message_id
    stampCallbackOwner(update, context, msg.message_id)

    return GIVE_OPTION


async def nextOption(update, context):
    if not await guardCallbackOwner(update, context):
        return

    position = context.user_data["position"] + 1
    context.user_data["position"] = position
    searchResult = context.user_data["output"]
    choice = context.user_data["choice"]    
    message=i18n.t("reelay.searchresults", count=len(searchResult))
    message += f"\n\n*{context.user_data['output'][position]['title']} ({context.user_data['output'][position]['year']})*"
    if overseerr.enabled():
        message += "\n" + overseerr.statusBadge(context.user_data['output'][position].get('status'))
    await context.bot.edit_message_text(
        message_id=context.user_data["title_update_msg"],
        chat_id=update.effective_message.chat_id,
        text=message,
        parse_mode=ParseMode.MARKDOWN,
    )
    
    if position < len(context.user_data["output"]) - 1:
        keyboard = [
                [
                    InlineKeyboardButton(
                        '\U00002795 '+i18n.t("reelay.Add"),
                        callback_data=i18n.t("reelay.Add")
                    ),
                ],[
                    InlineKeyboardButton(
                        '\U000023ED '+i18n.t("reelay.Next result"),
                        callback_data=i18n.t("reelay.Next result")
                    ),
                ],[
                    InlineKeyboardButton(
                        '\U0001F5D1 '+i18n.t("reelay.New"),
                        callback_data=i18n.t("reelay.New")
                    ),
                ],[
                    InlineKeyboardButton(
                        '\U0001F6D1 '+i18n.t("reelay.Stop"),
                        callback_data=i18n.t("reelay.Stop")
                    ),
                ],
            ]
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    '\U00002795 '+i18n.t("reelay.Add"),
                    callback_data=i18n.t("reelay.Add")
                ),
            ],[
                InlineKeyboardButton(
                    '\U0001F5D1 '+i18n.t("reelay.New"),
                    callback_data=i18n.t("reelay.New")
                ),
            ],[
                InlineKeyboardButton(
                    '\U0001F6D1 '+i18n.t("reelay.Stop"),
                    callback_data=i18n.t("reelay.Stop")
                ),
            ],
        ]
    markup = InlineKeyboardMarkup(keyboard)

    if context.user_data["photo_update_msg"]:
        await context.bot.delete_message(
            message_id=context.user_data["photo_update_msg"],
            chat_id=update.effective_message.chat_id,
        )
    
    try:
        img = await context.bot.sendPhoto(
            chat_id=update.effective_message.chat_id,
            photo=context.user_data["output"][position]["poster"],
        )
    except:
        context.user_data["photo_update_msg"] = None
    else:
        context.user_data["photo_update_msg"] = img.message_id
    
    await context.bot.delete_message(
        message_id=context.user_data["update_msg"],
        chat_id=update.effective_message.chat_id,
    )
    if choice == i18n.t("reelay.Movie"):
        message=i18n.t("reelay.messages.This", subjectWithArticle=i18n.t("reelay.MovieWithArticle").lower())
    else:
        message=i18n.t("reelay.messages.This", subjectWithArticle=i18n.t("reelay.SeriesWithArticle").lower())
    msg = await context.bot.send_message(
        chat_id=update.effective_message.chat_id, text=message, reply_markup=markup
    )
    context.user_data["update_msg"] = msg.message_id
    stampCallbackOwner(update, context, msg.message_id)
    return GIVE_OPTION


async def requestViaOverseerr(update, context):
    """Submit the selected title to Overseerr as the linked user. Hard-fails
    (no direct Sonarr/Radarr fallback) when the user isn't linked or Overseerr
    can't take the request, so nothing lands unattributed."""
    position = context.user_data["position"]
    choice = context.user_data["choice"]
    item = context.user_data["output"][position]
    tmdbId = item["id"]
    title = item["title"]

    async def fail(text):
        await context.bot.edit_message_text(
            message_id=context.user_data["update_msg"],
            chat_id=update.effective_message.chat_id,
            text=text,
        )
        clearUserData(context)
        return ConversationHandler.END

    scope = await resolveScope(update, context)
    if scope is None:
        return await fail(i18n.t("reelay.Overseerr.NoScope"))

    link = db.getSeerrLink(scope["chat_id"], update.effective_user.id)
    if link is None:
        return await fail(i18n.t("reelay.Overseerr.NotLinked"))

    media_type = "movie" if choice == i18n.t("reelay.Movie") else "tv"
    seasons = "all" if media_type == "tv" else None
    result = overseerr.createRequest(
        media_type, tmdbId, requested_by_seerr_id=link["seerr_user_id"], is4k=False, seasons=seasons
    )
    if not result:
        return await fail(i18n.t("reelay.Overseerr.RequestFailed", title=title))

    # Overseerr handles its own admin/approval notifications, so we don't
    # duplicate the adminNotifyId ping here.
    await context.bot.edit_message_text(
        message_id=context.user_data["update_msg"],
        chat_id=update.effective_message.chat_id,
        text=i18n.t("reelay.Overseerr.Requested", title=title),
    )
    await announceRequest(update, context, scope, title)
    clearUserData(context)
    return ConversationHandler.END


async def announceRequest(update, context, scope, title):
    """Post a public record of a request into the scope's configured request
    channel/topic (no-op if none configured, or if we're already there)."""
    if scope is None:
        return
    user = update.effective_user
    name = user.username or user.first_name or str(user.id)
    await channels.announce(
        context, scope["chat_id"], channels.CATEGORY_REQUESTS,
        i18n.t("reelay.Channels.RequestAnnounce", name=name, title=title),
        from_chat_id=update.effective_chat.id,
        from_thread_id=update.effective_message.message_thread_id,
    )


async def pathSerieMovie(update, context):
    if not await guardCallbackOwner(update, context):
        return

    # Overseerr path: it owns root folders and quality profiles, so skip those
    # prompts and submit the request straight to Overseerr.
    if overseerr.enabled():
        return await requestViaOverseerr(update, context)

    service = getService(context)
    paths = service.getRootFolders()
    excluded_root_folders = service.config.get("excludedRootFolders", [])
    paths = [p for p in paths if p["path"] not in excluded_root_folders]
    logger.debug(f"Excluded root folders: {excluded_root_folders}")
    context.user_data.update({"paths": [p["path"] for p in paths]})
    if len(paths) == 1:
        # There is only 1 path, so use it!
        logger.debug("Only found 1 path, so proceeding with that one...")
        context.user_data["path"] = paths[0]["path"]
        return await qualityProfileSerieMovie(update, context)
        
    keyboard = []
    for p in paths:
        pathtxt = p['path']
        if service.config.get("narrowRootFolderNames"):
            pathlst = p['path'].split("/")
            pathtxt = pathlst[len(pathlst)-1]
        free = format_bytes(p['freeSpace'])
        keyboard += [[
            InlineKeyboardButton(
            f"Path: {pathtxt}, Free: {free}",
            callback_data=f"Path: {p['path']}"
            ),
        ]]
    markup = InlineKeyboardMarkup(keyboard)

    await context.bot.edit_message_text(
        message_id=context.user_data["update_msg"],
        chat_id=update.effective_message.chat_id,
        text=i18n.t("reelay.Select a path"),
        reply_markup=markup,
    )
    stampCallbackOwner(update, context, context.user_data["update_msg"])
    return GIVE_PATHS


async def qualityProfileSerieMovie(update, context):
    if not await guardCallbackOwner(update, context):
        return

    if not context.user_data.get("path"):
        # Path selection should be in the update message
        path = None
        if update.callback_query is not None:
            try_path = update.callback_query.data.replace("Path: ", "").strip()
            if try_path in context.user_data.get("paths", {}):
                context.user_data["path"] = try_path
                path = try_path
        if path is None:
            logger.debug(
                f"Callback query [{update.callback_query.data.replace('Path: ', '').strip()}] doesn't match any of the paths. Sending paths for selection..."
            )
            return await pathSerieMovie(update, context)

    service = getService(context)

    excluded_quality_profiles = service.config.get("excludedQualityProfiles", [])
    qualityProfiles = service.getQualityProfiles()
    qualityProfiles = [q for q in qualityProfiles if q["name"] not in excluded_quality_profiles]
    
    context.user_data.update({"qualityProfiles": [q['id'] for q in qualityProfiles]})
    if len(qualityProfiles) == 1:
        # There is only 1 path, so use it!
        logger.debug("Only found 1 profile, so proceeding with that one...")
        context.user_data["qualityProfile"] = qualityProfiles[0]['id']
        return await selectSeasons(update, context)

    keyboard = []
    for q in qualityProfiles:
        keyboard += [[
            InlineKeyboardButton(
                f"Quality: {q['name']}",
                callback_data=f"Quality profile: {q['id']}"
            ),
        ]]
    markup = InlineKeyboardMarkup(keyboard)

    await context.bot.edit_message_text(
        message_id=context.user_data["update_msg"],
        chat_id=update.effective_message.chat_id,
        text=i18n.t("reelay.Select a quality"),
        reply_markup=markup,
    )
    stampCallbackOwner(update, context, context.user_data["update_msg"])
    return GIVE_QUALITY_PROFILES


async def selectSeasons(update, context):
    if not await guardCallbackOwner(update, context):
        return

    if not context.user_data.get("qualityProfile"):
        # Quality selection should be in the update message
        qualityProfile = None
        if update.callback_query is not None:
            try_qualityProfile = update.callback_query.data.replace("Quality profile: ", "").strip()
            if int(try_qualityProfile) in context.user_data.get("qualityProfiles", {}):
                context.user_data["qualityProfile"] = try_qualityProfile
                qualityProfile = int(try_qualityProfile)
        if qualityProfile is None:
            logger.debug(
                f"Callback query [{update.callback_query.data.replace('Quality profile: ', '').strip()}] doesn't match any of the quality profiles. Sending quality profiles for selection..."
            )
            return qualityProfileSerieMovie(update, context)

    service = getService(context)
    if service == radarr:
        return await addSerieMovie(update, context)
    
    position = context.user_data["position"]
    idnumber = context.user_data["output"][position]["id"]
    seasons = service.getSeasons(idnumber)
    seasonNumbers = [s["seasonNumber"] for s in seasons]
    context.user_data["seasons"] = seasonNumbers
    selectedSeasons = []

    keyboard = [[InlineKeyboardButton('\U0001F5D3 ' + i18n.t("reelay.Selected and future seasons"),callback_data="Season: Future and selected")]]
    for s in seasonNumbers:
        keyboard += [[
            InlineKeyboardButton(
                "\U00002705 " + f"{i18n.t('reelay.Season')} {s}",
                callback_data=f"Season: {s}"
            ),
        ]]
        selectedSeasons.append(int(s))

    keyboard += [[InlineKeyboardButton(i18n.t("reelay.Deselect all seasons"),callback_data=f"Season: None")]]

    markup = InlineKeyboardMarkup(keyboard)

    context.user_data["selectedSeasons"] = selectedSeasons

    await context.bot.edit_message_text(
        message_id=context.user_data["update_msg"],
        chat_id=update.effective_message.chat_id,
        text=i18n.t("reelay.Select from which season"),
        reply_markup=markup,
    )
    stampCallbackOwner(update, context, context.user_data["update_msg"])
    return SELECT_SEASONS

async def checkSeasons(update, context):
    if not await guardCallbackOwner(update, context):
        return

    choice = context.user_data["choice"]
    seasons = context.user_data["seasons"]
    selectedSeasons = []
    if "selectedSeasons" in context.user_data:
        selectedSeasons = context.user_data["selectedSeasons"]
    
    if choice == i18n.t("reelay.Series"):
        if update.callback_query is not None:
            insertSeason = update.callback_query.data.replace("Season: ", "").strip()
            if insertSeason == "Future and selected":
                seasonsSelected = []
                for s in seasons:
                    monitored = False
                    if s in selectedSeasons:
                        monitored = True
                    seasonsSelected.append(
                        {
                            "seasonNumber": s,
                            "monitored": monitored,
                        }
                    )
                logger.debug(f"Seasons {seasonsSelected} have been selected.")
                
                context.user_data["selectedSeasons"] = selectedSeasons
                return await addSerieMovie(update, context)
              
            else:
                if insertSeason == "All":
                    for s in seasons:
                        if s not in selectedSeasons:
                            selectedSeasons.append(s)
                elif insertSeason == "None":
                    for s in seasons:
                        if s in selectedSeasons:
                            selectedSeasons.remove(s)
                elif int(insertSeason) not in selectedSeasons:
                    selectedSeasons.append(int(insertSeason))
                else:
                    selectedSeasons.remove(int(insertSeason))
                    
                context.user_data["selectedSeasons"] = selectedSeasons
                keyboard = [[InlineKeyboardButton('\U0001F5D3 ' + i18n.t("reelay.Selected and future seasons"),callback_data="Season: Future and selected")]]
                for s in seasons:
                    if s in selectedSeasons: 
                        season = "\U00002705 " + f"{i18n.t('reelay.Season')} {s}" 
                    else:
                        season = "\U00002B1C " + f"{i18n.t('reelay.Season')} {s}"

                    keyboard.append([
                        InlineKeyboardButton(
                            season,
                            callback_data=f"Season: {s}"
                        )
                    ])
                
                if len(selectedSeasons) == len(seasons):
                    keyboard += [[InlineKeyboardButton(i18n.t("reelay.Deselect all seasons"),callback_data=f"Season: None")]]
                else:
                    keyboard += [[InlineKeyboardButton(i18n.t("reelay.Select all seasons"),callback_data=f"Season: All")]]

                markup = InlineKeyboardMarkup(keyboard)

                await context.bot.edit_message_text(
                    message_id=context.user_data["update_msg"],
                    chat_id=update.effective_message.chat_id,
                    text=i18n.t("reelay.Select from which season"),
                    reply_markup=markup,
                )
                stampCallbackOwner(update, context, context.user_data["update_msg"])
                return SELECT_SEASONS
            
        if selectedSeasons is None:
            logger.debug(
                f"Callback query [{update.callback_query.data.replace('From season: ', '').strip()}] doesn't match any of the season options. Sending seasons for selection..."
            )
            return await checkSeasons(update, context)
        
async def addSerieMovie(update, context):
    position = context.user_data["position"]
    choice = context.user_data["choice"]
    idnumber = context.user_data["output"][position]["id"]
    path = context.user_data["path"]
    service = getService(context)
    
    if choice == i18n.t("reelay.Series"):
        seasons = context.user_data["seasons"]
        selectedSeasons = context.user_data["selectedSeasons"]
        seasonsSelected = []
        for s in seasons:
            monitored = False
            if s in selectedSeasons:
                monitored = True
                
            seasonsSelected.append(
                {
                    "seasonNumber": s,
                    "monitored": monitored,
                }
            )
        logger.debug(f"Seasons {seasonsSelected} have been selected.")
    
    qualityProfile = context.user_data["qualityProfile"]

    #Add tag for user
    #TODO (creation does not work right now, creation should be manual)
    # Tag with the requesting USER's id, not the chat id -- in a group chat
    # the chat id is the group's, which would tag every member's request the
    # same. In a 1:1 DM the two are equal, so this is a no-op there.
    requesterId = update.effective_user.id
    tags = []
    if service.config.get("addRequesterIdTag"):
        if str(requesterId) not in [str(t["label"]) for t in service.getTags()]:
            service.createTag(str(requesterId))
        for t in service.getTags():
            if str(t["label"]) == str(requesterId):
                tags.append(str(t["id"]))
    if not tags:
        tags = [int(t["id"]) for t in service.getTags() if t["label"] in service.config.get("defaultTags", [])]
    logger.debug(f"Tags {tags} have been selected.")
    
    if not service.inLibrary(idnumber):
        if choice == i18n.t("reelay.Movie"):
            added = service.addToLibrary(idnumber, path, qualityProfile, tags)
        else:
            added = service.addToLibrary(idnumber, path, qualityProfile, tags, seasonsSelected)
        
        if added:
            if choice == i18n.t("reelay.Movie"):
                message=i18n.t("reelay.messages.AddSuccess", subjectWithArticle=i18n.t("reelay.MovieWithArticle"))
            else:
                message=i18n.t("reelay.messages.AddSuccess", subjectWithArticle=i18n.t("reelay.SeriesWithArticle"))
            await context.bot.edit_message_text(
                message_id=context.user_data["update_msg"],
                chat_id=update.effective_message.chat_id,
                text=message,
            )
            if not checkAllowed(update,"admin") and config.get("adminNotifyId") is not None:
                adminNotifyId = config.get("adminNotifyId")
                if choice == i18n.t("reelay.Movie"):
                    message2=i18n.t("reelay.Notifications.AddSuccess", subjectWithArticle=i18n.t("reelay.MovieWithArticle"),title=context.user_data['output'][position]['title'],first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
                else:
                    message2=i18n.t("reelay.Notifications.AddSuccess", subjectWithArticle=i18n.t("reelay.SeriesWithArticle"),title=context.user_data['output'][position]['title'],first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
                await context.bot.send_message(
                    chat_id=adminNotifyId, text=message2
                )
            scope = await resolveScope(update, context)
            await announceRequest(update, context, scope, context.user_data['output'][position]['title'])
            clearUserData(context)
            return ConversationHandler.END
        else:
            if choice == i18n.t("reelay.Movie"):
                message=i18n.t("reelay.messages.AddFailed", subjectWithArticle=i18n.t("reelay.MovieWithArticle").lower())
            else:
                message=i18n.t("reelay.messages.AddFailed", subjectWithArticle=i18n.t("reelay.SeriesWithArticle").lower())
            await context.bot.edit_message_text(
                message_id=context.user_data["update_msg"],
                chat_id=update.effective_message.chat_id,
                text=message,
            )
            if not checkAllowed(update,"admin") and config.get("adminNotifyId") is not None:
                adminNotifyId = config.get("adminNotifyId")
                if choice == i18n.t("reelay.Movie"):
                    message2=i18n.t("reelay.Notifications.AddFailed", subjectWithArticle=i18n.t("reelay.MovieWithArticle"),title=context.user_data['output'][position]['title'],first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
                else:
                    message2=i18n.t("reelay.Notifications.AddFailed", subjectWithArticle=i18n.t("reelay.SeriesWithArticle"),title=context.user_data['output'][position]['title'],first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
                await context.bot.send_message(
                    chat_id=adminNotifyId, text=message2
                )
            clearUserData(context)
            return ConversationHandler.END
    else:
        if choice == i18n.t("reelay.Movie"):
            message=i18n.t("reelay.messages.Exist", subjectWithArticle=i18n.t("reelay.MovieWithArticle"))
        else:
            message=i18n.t("reelay.messages.Exist", subjectWithArticle=i18n.t("reelay.SeriesWithArticle"))
        await context.bot.edit_message_text(
            message_id=context.user_data["update_msg"],
            chat_id=update.effective_message.chat_id,
            text=message,
        )
            
        if not checkAllowed(update,"admin") and config.get("adminNotifyId") is not None:
            adminNotifyId = config.get("adminNotifyId")
            if choice == i18n.t("reelay.Movie"):
                message2=i18n.t("reelay.Notifications.Exist", subjectWithArticle=i18n.t("reelay.MovieWithArticle"),title=context.user_data['output'][position]['title'],first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
            else:
                message2=i18n.t("reelay.Notifications.Exist", subjectWithArticle=i18n.t("reelay.SeriesWithArticle"),title=context.user_data['output'][position]['title'],first_name=update.effective_message.chat.first_name, chat_id=update.effective_message.chat.id)
            await context.bot.send_message(
                chat_id=adminNotifyId, text=message2
            )
        clearUserData(context)
        return ConversationHandler.END



async def help(update, context):
    if config.get("enableAllowlist") and not checkAllowed(update,"regular"):
        #When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return ConversationHandler.END
    
    await context.bot.send_message(
        chat_id=update.effective_message.chat_id, text=i18n.t("reelay.Help",
            help=config["entrypointHelp"],
            authenticate=config["entrypointAuth"],
            add=config["entrypointAdd"],
            delete=config["entrypointDelete"],
            movie=i18n.t("reelay.Movie").lower(),
            serie=i18n.t("reelay.Series").lower(),
            allSeries=config["entrypointAllSeries"],
            allMovies=config["entrypointAllMovies"],
            transmission=config["entrypointTransmission"],
            sabnzbd=config["entrypointSabnzbd"],
        )
    )
    return ConversationHandler.END





def run():
    loop = asyncio.get_event_loop()
    if loop.run_until_complete(startCheck()):
        main()
        loop.close()
    else:
        import sys
        sys.exit(0)
