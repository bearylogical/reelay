import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop

from . import db
from . import logger
from . import overseerr
from .config import config
from .translations import i18n

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.onboarding", logLevel, config.get("logToConsole", False))

# Inline keyboards get unwieldy past a couple dozen buttons; a household
# Overseerr rarely has more. Anything beyond this is truncated (a searchable
# picker would be the enhancement if a deployment needs it).
SEERR_PICKER_LIMIT = 25


async def join(update, context):
    """/join <code> - request to join an existing scope from a DM. Kept as a
    separate command (rather than reusing Telegram's /start deep-link
    convention) because Reelay's /start is already the add-flow entry
    point."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinDmOnly"))
        return

    args = context.args
    if not args:
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinUsage"))
        return

    scope = db.getScopeByInviteCode(args[0].strip())
    if not scope:
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinBadCode"))
        return

    user = update.effective_user
    if scope["join_policy"] == "auto":
        db.upsertMembership(scope["chat_id"], user.id, user.username, status="approved")
        db.approveMembership(scope["chat_id"], user.id, approved_by="auto")
        await update.message.reply_text(
            i18n.t("reelay.Onboarding.JoinApproved", title=scope.get("title") or scope["chat_id"])
        )
        await askReminderThreshold(update, context)
        await askAnonymize(update, context)
        # No admin in the loop on an auto-join, so let the user self-select
        # which Overseerr/Plex account is theirs.
        await sendSeerrPicker(
            context, user.id, scope["chat_id"], user.id,
            user.username or user.first_name or str(user.id),
        )
        return

    db.upsertMembership(scope["chat_id"], user.id, user.username, status="pending")
    await update.message.reply_text(i18n.t("reelay.Onboarding.JoinPending", title=scope.get("title") or scope["chat_id"]))

    keyboard = [[
        InlineKeyboardButton(i18n.t("reelay.Onboarding.Approve"), callback_data=f"approve_join_{scope['chat_id']}_{user.id}"),
        InlineKeyboardButton(i18n.t("reelay.Onboarding.Deny"), callback_data=f"deny_join_{scope['chat_id']}_{user.id}"),
    ]]
    markup = InlineKeyboardMarkup(keyboard)
    for admin in db.getApprovedAdmins(scope["chat_id"]):
        try:
            await context.bot.send_message(
                chat_id=admin["user_id"],
                text=i18n.t(
                    "reelay.Onboarding.ApprovalRequest",
                    name=user.username or user.first_name or str(user.id),
                    title=scope.get("title") or scope["chat_id"],
                ),
                reply_markup=markup,
            )
        except Exception:
            logger.warning(f"Could not DM admin {admin['user_id']} about pending join request.")


async def handleApproval(update, context):
    query = update.callback_query
    action, _, scope_chat_id, target_user_id = query.data.split("_", 3)  # "approve"/"deny", "join", scope, user

    membership = db.getMembership(scope_chat_id, target_user_id)
    if membership is None:
        await query.answer(i18n.t("reelay.Onboarding.RequestGone"), show_alert=True)
        return

    admin_membership = db.getMembership(scope_chat_id, update.effective_user.id)
    if not admin_membership or admin_membership["role"] != "admin" or admin_membership["status"] != "approved":
        await query.answer(i18n.t("reelay.NotAdmin"), show_alert=True)
        return

    display_name = membership.get("username") or target_user_id

    if action == "approve":
        db.approveMembership(scope_chat_id, target_user_id, approved_by=update.effective_user.id)
        await query.edit_message_text(i18n.t("reelay.Onboarding.Approved", name=display_name))
        scope = db.getScope(scope_chat_id)
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=i18n.t("reelay.Onboarding.JoinApproved", title=scope.get("title") or scope_chat_id),
            )
        except Exception:
            pass
        await askReminderThresholdFor(context, target_user_id)
        await askAnonymizeFor(context, target_user_id)
        # Ask the approving admin to map this member to their Overseerr/Plex
        # account, so request attribution and watch tracking work for them.
        await sendSeerrPicker(context, update.effective_user.id, scope_chat_id, target_user_id, display_name)
    else:
        db.denyMembership(scope_chat_id, target_user_id)
        await query.edit_message_text(i18n.t("reelay.Onboarding.Denied", name=display_name))


# --- Overseerr/Plex account linkage ------------------------------------------

async def sendSeerrPicker(context, recipient_id, scope_chat_id, target_user_id, target_display):
    """DM `recipient_id` an inline keyboard of Overseerr users to link
    `target_user_id` to. Silently no-ops when Overseerr isn't configured."""
    if not overseerr.enabled():
        return
    users = overseerr.getUsers()
    if not users:
        logger.warning("Overseerr is enabled but returned no users for linkage picker.")
        return

    keyboard = []
    for u in users[:SEERR_PICKER_LIMIT]:
        label = u["displayName"]
        if u.get("plexUsername") and u["plexUsername"] != u["displayName"]:
            label += f" (Plex: {u['plexUsername']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"slink_{scope_chat_id}_{target_user_id}_{u['id']}")])
    keyboard.append([InlineKeyboardButton(i18n.t("reelay.Onboarding.LinkSkip"), callback_data=f"sskip_{scope_chat_id}_{target_user_id}")])

    try:
        await context.bot.send_message(
            chat_id=recipient_id,
            text=i18n.t("reelay.Onboarding.LinkPrompt", name=target_display),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.warning(f"Could not DM Overseerr linkage picker to {recipient_id}.")


async def handleSeerrLink(update, context):
    query = update.callback_query
    parts = query.data.split("_", 3)
    if parts[0] == "sskip":
        _, scope_chat_id, target_user_id = query.data.split("_", 2)
        if not _mayLink(update.effective_user.id, scope_chat_id, target_user_id):
            await query.answer(i18n.t("reelay.NotAdmin"), show_alert=True)
            return
        await query.edit_message_text(i18n.t("reelay.Onboarding.LinkSkipped"))
        return

    _, scope_chat_id, target_user_id, seerr_id = parts
    if not _mayLink(update.effective_user.id, scope_chat_id, target_user_id):
        await query.answer(i18n.t("reelay.NotAdmin"), show_alert=True)
        return

    seerr_username, seerr_email = None, None
    for u in overseerr.getUsers():
        if str(u["id"]) == seerr_id:
            seerr_username = u["displayName"]
            seerr_email = u.get("email")
            break

    db.linkSeerr(scope_chat_id, target_user_id, int(seerr_id),
                 seerr_username=seerr_username, seerr_email=seerr_email, mode="api")
    await query.edit_message_text(i18n.t("reelay.Onboarding.LinkDone", account=seerr_username or seerr_id))


def _mayLink(actor_id, scope_chat_id, target_user_id):
    """A user may link themselves; an approved admin of the scope may link
    anyone in it."""
    if str(actor_id) == str(target_user_id):
        m = db.getMembership(scope_chat_id, actor_id)
        return bool(m and m["status"] == "approved")
    admin = db.getMembership(scope_chat_id, actor_id)
    return bool(admin and admin["role"] == "admin" and admin["status"] == "approved")


async def linkme(update, context):
    """/linkme - self-service: link your own Telegram account to your
    Overseerr/Plex account. Works from a DM."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinDmOnly"))
        return
    if not overseerr.enabled():
        await update.message.reply_text(i18n.t("reelay.Onboarding.LinkNoOverseerr"))
        return

    memberships = db.getApprovedMemberships(update.effective_user.id)
    if not memberships:
        await update.message.reply_text(i18n.t("reelay.Onboarding.NoMembership"))
        return

    # Link within the user's active/only scope -- the same resolution the add
    # flow uses. For a single-group deployment this is unambiguous.
    scope_chat_id = memberships[0]["scope_chat_id"]
    active = db.getActiveScope(update.effective_user.id)
    if active and any(m["scope_chat_id"] == active for m in memberships):
        scope_chat_id = active

    await sendSeerrPicker(
        context, update.effective_user.id, scope_chat_id, update.effective_user.id,
        update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id),
    )


async def requestlink(update, context):
    """/requestlink - admin command: DM approved members of the admin's scope
    who haven't linked their Overseerr/Plex account yet, asking them to."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinDmOnly"))
        return
    if not overseerr.enabled():
        await update.message.reply_text(i18n.t("reelay.Onboarding.LinkNoOverseerr"))
        return

    memberships = db.getApprovedMemberships(update.effective_user.id)
    if not memberships:
        await update.message.reply_text(i18n.t("reelay.Onboarding.NoMembership"))
        return

    # Same active-scope resolution as /linkme, so an admin in several groups
    # targets whichever one they last /switch-ed to.
    scope_chat_id = memberships[0]["scope_chat_id"]
    active = db.getActiveScope(update.effective_user.id)
    if active and any(m["scope_chat_id"] == active for m in memberships):
        scope_chat_id = active

    admin_membership = next((m for m in memberships if m["scope_chat_id"] == scope_chat_id), None)
    if not admin_membership or admin_membership["role"] != "admin":
        await update.message.reply_text(i18n.t("reelay.NotAdmin"))
        return

    scope = db.getScope(scope_chat_id)
    title = scope.get("title") or scope_chat_id

    unlinked = db.getApprovedMembersWithoutSeerrLink(scope_chat_id)
    if not unlinked:
        await update.message.reply_text(i18n.t("reelay.Onboarding.RequestLinkNoneUnlinked", title=title))
        return

    keyboard = [
        [InlineKeyboardButton(
            m.get("username") or m["user_id"],
            callback_data=f"reqlink_{scope_chat_id}_{m['user_id']}",
        )]
        for m in unlinked
    ]
    keyboard.append([InlineKeyboardButton(
        i18n.t("reelay.Onboarding.RequestLinkAllButton", count=len(unlinked)),
        callback_data=f"reqlinkall_{scope_chat_id}",
    )])
    await update.message.reply_text(
        i18n.t("reelay.Onboarding.RequestLinkPrompt", title=title),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handleRequestLink(update, context):
    query = update.callback_query

    if query.data.startswith("reqlinkall_"):
        scope_chat_id = query.data[len("reqlinkall_"):]
        if not _mayRequestLink(update.effective_user.id, scope_chat_id):
            await query.answer(i18n.t("reelay.NotAdmin"), show_alert=True)
            return
        unlinked = db.getApprovedMembersWithoutSeerrLink(scope_chat_id)
        sent = 0
        for m in unlinked:
            ok = await _sendLinkNudge(context, scope_chat_id, m["user_id"], m.get("username") or m["user_id"])
            sent += 1 if ok else 0
        await query.edit_message_text(i18n.t("reelay.Onboarding.RequestLinkSentAll", count=sent))
        return

    _, scope_chat_id, target_user_id = query.data.split("_", 2)
    if not _mayRequestLink(update.effective_user.id, scope_chat_id):
        await query.answer(i18n.t("reelay.NotAdmin"), show_alert=True)
        return

    membership = db.getMembership(scope_chat_id, target_user_id)
    display_name = (membership.get("username") if membership else None) or target_user_id
    if await _sendLinkNudge(context, scope_chat_id, target_user_id, display_name):
        await query.edit_message_text(i18n.t("reelay.Onboarding.RequestLinkSent", name=display_name))
    else:
        await query.answer(i18n.t("reelay.Onboarding.RequestLinkFailed", name=display_name), show_alert=True)


def _mayRequestLink(actor_id, scope_chat_id):
    admin = db.getMembership(scope_chat_id, actor_id)
    return bool(admin and admin["role"] == "admin" and admin["status"] == "approved")


async def _sendLinkNudge(context, scope_chat_id, target_user_id, target_display):
    """DM `target_user_id` asking them to link their Overseerr/Plex account,
    then follow up with the picker so they can do it in one place. Returns
    whether the DM went through (fails if the user never started a chat with
    the bot)."""
    scope = db.getScope(scope_chat_id)
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=i18n.t("reelay.Onboarding.LinkNudge", title=scope.get("title") or scope_chat_id),
        )
    except Exception:
        logger.warning(f"Could not DM link request to {target_user_id}.")
        return False
    await sendSeerrPicker(context, target_user_id, scope_chat_id, target_user_id, target_display)
    return True


async def askReminderThreshold(update, context):
    await askReminderThresholdFor(context, update.effective_user.id)


async def askReminderThresholdFor(context, telegram_user_id):
    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=i18n.t("reelay.Onboarding.AskReminderDays"),
        )
    except Exception:
        logger.warning(f"Could not DM {telegram_user_id} the onboarding reminder question.")


async def catchReminderThresholdReply(update, context):
    """Registered as a low-numbered handler group so it gets first look at
    DM text. Whether a user is "awaiting" an answer is a persisted signal
    (an approved membership with reminder_threshold_days IS NULL), not an
    in-memory flag -- this survives a bot restart, and avoids needing to
    mutate a DIFFERENT user's context.user_data from an admin's own handler
    (application.user_data itself is read-only in PTB v20 for new keys)."""
    if update.effective_chat.type != "private" or update.message is None or update.message.text is None:
        return

    pending = db.getMembershipsAwaitingReminderAnswer(update.effective_user.id)
    if not pending:
        return

    text = update.message.text.strip()
    try:
        days = int(text)
        if not (0 <= days <= 30):
            raise ValueError
    except ValueError:
        await update.message.reply_text(i18n.t("reelay.Onboarding.ReminderDaysInvalid"))
        raise ApplicationHandlerStop

    for m in pending:
        db.setReminderThreshold(m["scope_chat_id"], update.effective_user.id, days)

    if days == 0:
        await update.message.reply_text(i18n.t("reelay.Onboarding.ReminderDisabled"))
    else:
        await update.message.reply_text(i18n.t("reelay.Onboarding.ReminderSet", days=days))
    raise ApplicationHandlerStop


async def remindme(update, context):
    """/remindme <days> - change the reminder threshold at any time."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinDmOnly"))
        return

    memberships = db.getApprovedMemberships(update.effective_user.id)
    if not memberships:
        await update.message.reply_text(i18n.t("reelay.Onboarding.NoMembership"))
        return

    if not context.args:
        await update.message.reply_text(i18n.t("reelay.Onboarding.RemindmeUsage"))
        return

    try:
        days = int(context.args[0])
        if not (0 <= days <= 30):
            raise ValueError
    except ValueError:
        await update.message.reply_text(i18n.t("reelay.Onboarding.ReminderDaysInvalid"))
        return

    for m in memberships:
        db.setReminderThreshold(m["scope_chat_id"], update.effective_user.id, days)

    if days == 0:
        await update.message.reply_text(i18n.t("reelay.Onboarding.ReminderDisabled"))
    else:
        await update.message.reply_text(i18n.t("reelay.Onboarding.ReminderSet", days=days))


# --- Anonymize-requests preference -------------------------------------------

async def askAnonymize(update, context):
    await askAnonymizeFor(context, update.effective_user.id)


async def askAnonymizeFor(context, telegram_user_id):
    await _sendAnonymizePicker(context.bot, telegram_user_id, i18n.t("reelay.Onboarding.AskAnonymize"))


async def _sendAnonymizePicker(bot, telegram_user_id, prompt):
    keyboard = [[
        InlineKeyboardButton(i18n.t("reelay.Onboarding.AnonShowName"), callback_data="anon_no"),
        InlineKeyboardButton(i18n.t("reelay.Onboarding.AnonKeepPrivate"), callback_data="anon_yes"),
    ]]
    try:
        await bot.send_message(
            chat_id=telegram_user_id,
            text=prompt,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.warning(f"Could not DM {telegram_user_id} the onboarding anonymize question.")


async def handleAnonymizeChoice(update, context):
    """Callback for the anon_yes/anon_no picker. Not scope-specific in its
    callback_data -- like the reminder-threshold reply, it applies to
    whichever of this user's memberships are still awaiting an answer."""
    query = update.callback_query
    hide_name = query.data == "anon_yes"
    pending = db.getMembershipsAwaitingAnonymizeAnswer(query.from_user.id)
    for m in pending:
        db.setAnonymizeRequests(m["scope_chat_id"], query.from_user.id, hide_name)

    await query.answer()
    if hide_name:
        await query.edit_message_text(i18n.t("reelay.Onboarding.AnonSetHidden"))
    else:
        await query.edit_message_text(i18n.t("reelay.Onboarding.AnonSetShown"))


async def sendAnonymizeBackfill(bot):
    """DM the anonymize-requests picker to approved members who joined before
    this preference existed. Safe to call on every startup -- once a
    membership is answered it stops matching
    getApprovedMembersAwaitingAnonymizeAnswer, so no repeat DMs."""
    pending = db.getApprovedMembersAwaitingAnonymizeAnswer()
    seen = set()
    for m in pending:
        if m["user_id"] in seen:
            continue
        seen.add(m["user_id"])
        await _sendAnonymizePicker(bot, m["user_id"], i18n.t("reelay.Onboarding.AskAnonymizeLegacy"))


async def anonymize(update, context):
    """/anonymize on|off - change the request-attribution preference at any time."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(i18n.t("reelay.Onboarding.JoinDmOnly"))
        return

    memberships = db.getApprovedMemberships(update.effective_user.id)
    if not memberships:
        await update.message.reply_text(i18n.t("reelay.Onboarding.NoMembership"))
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text(i18n.t("reelay.Onboarding.AnonymizeUsage"))
        return

    hide_name = context.args[0].lower() == "on"
    for m in memberships:
        db.setAnonymizeRequests(m["scope_chat_id"], update.effective_user.id, hide_name)

    if hide_name:
        await update.message.reply_text(i18n.t("reelay.Onboarding.AnonSetHidden"))
    else:
        await update.message.reply_text(i18n.t("reelay.Onboarding.AnonSetShown"))
