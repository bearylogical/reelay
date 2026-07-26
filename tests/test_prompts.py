"""Tests for the bot's user-facing copy: that every translation key the code
asks for actually exists, and that /help only offers what the caller can use."""

import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

import reelay.bot as bot
import reelay.db as db
import reelay.onboarding as onboarding
from reelay.translations import i18n

SRC = pathlib.Path(__file__).resolve().parent.parent / "reelay"

# Literal keys only -- i18n.t(err) and friends are resolved at runtime from a
# variable and can't be checked statically.
KEY_RE = re.compile(r'i18n\.t\(\s*"(reelay\.[A-Za-z0-9_. ]+)"')

# Every placeholder any string might want. Passing them all to every key is
# fine: i18n ignores the ones a given string doesn't use.
FILLERS = dict(
    name="n", title="t", code="c", link="l", role="member", days=3, count=1,
    chat_id="1", first_name="f", account="a", subject="s", subjectWithArticle="the movie",
    categories="requests, updates", category="requests", where="here", routes="r",
    thread="1", missingKeys="k", wrongValues="v", help="help", add="start",
    delete="delete", allSeries="allSeries", allMovies="allMovies",
    authenticate="auth", movie="movie", serie="series",
    transmission="transmission", sabnzbd="sabnzbd", lines="x",
)


def _referenced_keys():
    keys = set()
    for path in SRC.glob("*.py"):
        keys.update(KEY_RE.findall(path.read_text(encoding="utf8")))
    return sorted(keys)


def test_every_referenced_translation_key_resolves():
    """A missing key isn't an exception -- python-i18n hands the key itself
    back, so the user is shown a raw "reelay.SwitchedTo". That shipped once;
    this is the check that stops it shipping again."""
    missing = [k for k in _referenced_keys() if i18n.t(k, **FILLERS) == k]
    assert missing == []


def test_no_unresolved_placeholders_in_referenced_strings():
    """Catches a typo'd placeholder (%{c.hat_id}) reaching a real message."""
    leaked = [k for k in _referenced_keys() if "%{" in str(i18n.t(k, **FILLERS))]
    assert leaked == []


# --- /help ---------------------------------------------------------------------

def test_help_hides_admin_commands_from_members():
    text = "\n".join(bot._helpSections("member", in_group=False, scope=None))
    assert "/join" in text and "/linkme" in text
    assert "/routehere" not in text and "/requestlink" not in text
    # Library/queue commands are editor+ only
    assert "/allSeries" not in text


def test_help_shows_admin_section_to_admins():
    text = "\n".join(bot._helpSections("admin", in_group=False, scope=None))
    assert "/routehere" in text and "/requestlink" in text and "/allSeries" in text


def test_help_for_a_stranger_leads_with_joining():
    text = "\n".join(bot._helpSections(None, in_group=False, scope=None))
    assert "/join" in text
    # Nothing they can't do yet
    assert "/linkme" not in text and "/remindme" not in text


def test_help_in_unclaimed_group_says_how_to_claim():
    text = "\n".join(bot._helpSections(None, in_group=True, scope=None))
    assert "/claim" in text


def test_help_in_group_reflects_the_group_requests_toggle():
    db.upsertScope("-100111", title="Fam")
    scope = db.getScope("-100111")

    off = "\n".join(bot._helpSections("member", in_group=True, scope=scope))
    assert "DM" in off

    db.setFeature("-100111", db.FEATURE_GROUP_REQUESTS, True)
    on = "\n".join(bot._helpSections("member", in_group=True, scope=db.getScope("-100111")))
    assert "/start" in on


def test_help_omits_disabled_download_clients(monkeypatch):
    monkeypatch.setitem(bot.config, "transmission", {"enable": False})
    monkeypatch.setitem(bot.config, "sabnzbd", {"enable": False})
    text = "\n".join(bot._helpSections("admin", in_group=False, scope=None))
    assert "/transmission" not in text and "/sabnzbd" not in text


# --- Reminder-threshold buttons -------------------------------------------------

def _query(data, user_id=1):
    q = MagicMock()
    q.data = data
    q.from_user.id = user_id
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = q
    return update, q


def test_reminder_button_sets_the_threshold():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "a", status="approved")
    db.approveMembership("-100111", "1", approved_by="x")

    update, q = _query("remdays_7")
    asyncio.run(onboarding.handleReminderDaysChoice(update, None))
    assert db.getMembership("-100111", "1")["reminder_threshold_days"] == 7
    assert "7" in q.edit_message_text.call_args.args[0]


def test_reminder_button_zero_disables():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "a", status="approved")
    db.approveMembership("-100111", "1", approved_by="x")

    update, q = _query("remdays_0")
    asyncio.run(onboarding.handleReminderDaysChoice(update, None))
    assert db.getMembership("-100111", "1")["reminder_threshold_days"] == 0


def test_reminder_button_still_applies_after_the_question_was_answered():
    """A late tap must not report success while writing nothing."""
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "a", status="approved")
    db.approveMembership("-100111", "1", approved_by="x")
    db.setReminderThreshold("-100111", "1", 3)
    assert db.getMembershipsAwaitingReminderAnswer("1") == []

    update, _ = _query("remdays_14")
    asyncio.run(onboarding.handleReminderDaysChoice(update, None))
    assert db.getMembership("-100111", "1")["reminder_threshold_days"] == 14
