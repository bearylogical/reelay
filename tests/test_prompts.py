"""Tests for the bot's user-facing copy: that every translation key the code
asks for actually exists, and that /help only offers what the caller can use."""

import asyncio
import contextlib
import copy
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import reelay.bot as bot
import reelay.db as db
import reelay.onboarding as onboarding
import reelay.translations as translations
from reelay.definitions import THEMES
from reelay.translations import i18n

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "reelay"
LANG = ROOT / "translations"

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
    transmission="transmission", sabnzbd="sabnzbd", lines="x", fact="cats sleep a lot",
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


# --- Personality themes ---------------------------------------------------------
#
# A theme is an overlay in a different voice, so the failure mode isn't a
# crash -- it's a string that quietly stops saying the thing it was there to
# say. These check the overlay against the copy it replaces.

PLACEHOLDER_RE = re.compile(r"%\{(\w+)\}")
# Commands as they appear in copy: /start, /%{add}, /app. The lookbehind keeps
# "Plex/Overseerr" and "Radarr/Sonarr" out of it.
COMMAND_RE = re.compile(r"(?<![\w/])/(?:%\{\w+\}|[a-zA-Z]+)")

THEMED_LOCALE = "en-us"


def _flatten(dic, prefix=""):
    """Leaf strings of a loaded translation file, keyed 'Help.Header'-style."""
    flat = {}
    for key, value in dic.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _load(path):
    return _flatten(yaml.safe_load(path.read_text(encoding="utf8"))[THEMED_LOCALE])


def _variants(value):
    """Every line a key can produce. A themed value is either one string or a
    list to pick from, and a guard that only looked at the first line would
    wave through a broken variant that shows up one time in three."""
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _dropped(strings, pattern):
    """Per key, what `pattern` finds in the plain copy but not in some themed
    variant of it -- with the variant that lost it, since with a list of lines
    "it's missing" isn't enough to go and fix it."""
    losses = {}
    for key, themed in strings.items():
        if key not in BASE_STRINGS:
            continue
        expected = set(pattern.findall(str(BASE_STRINGS[key])))
        for line in _variants(themed):
            missing = sorted(expected - set(pattern.findall(line)))
            if missing:
                losses[f"{key}: {line[:40]}"] = missing
    return losses


BASE_STRINGS = _load(LANG / f"reelay.{THEMED_LOCALE}.yml")
OVERLAY_FILES = [(name, LANG / "themes" / name / f"reelay.{THEMED_LOCALE}.yml")
                 for name in THEMES if name != "default"]
OVERLAY_STRINGS = [(name, _load(path)) for name, path in OVERLAY_FILES if path.exists()]


@contextlib.contextmanager
def _themed(name):
    """Apply a theme for the duration of the block. i18n caches every string it
    has resolved in a module-level container, so the cache is emptied on the
    way in and restored on the way out -- otherwise the first themed lookup
    would leak into every later test in the session."""
    from i18n import translations as store
    load_path, container = list(i18n.load_path), copy.deepcopy(store.container)
    store.container.clear()
    try:
        yield translations.applyTheme(name)
    finally:
        i18n.load_path[:] = load_path
        store.container.clear()
        store.container.update(container)


def test_every_declared_theme_ships_an_overlay():
    missing = [name for name, path in OVERLAY_FILES if not path.exists()]
    assert missing == []


@pytest.mark.parametrize("name,strings", OVERLAY_STRINGS)
def test_themes_only_override_keys_that_exist(name, strings):
    """A themed key the base file doesn't have is dead copy: nothing reads it,
    and the string it was meant to replace still shows the plain wording."""
    assert [k for k in strings if k not in BASE_STRINGS] == []


@pytest.mark.parametrize("name,strings", OVERLAY_STRINGS)
def test_every_variant_keeps_every_placeholder(name, strings):
    """Drop %{title} in a themed line and the user is told something is ready
    without being told what."""
    assert _dropped(strings, PLACEHOLDER_RE) == {}


@pytest.mark.parametrize("name,strings", OVERLAY_STRINGS)
def test_every_variant_keeps_the_commands_it_mentions(name, strings):
    """The jokes are cosmetic; "send /linkme" is not. Every themed line has to
    still name every command the plain one told the user to run."""
    assert _dropped(strings, COMMAND_RE) == {}


@pytest.mark.parametrize("name,strings", OVERLAY_STRINGS)
def test_variant_lists_have_no_duplicate_lines(name, strings):
    """A line repeated inside one list is a copy-paste, and it quietly doubles
    that line's odds of being the one you see."""
    duplicated = {k: v for k, v in strings.items()
                  if isinstance(v, list) and len(set(v)) != len(v)}
    assert duplicated == {}


def test_default_theme_is_the_plain_copy():
    assert translations.themeDir("default") is None
    assert translations.themeDir(None) is None
    with _themed("default") as applied:
        assert applied is False
        assert i18n.t("reelay.Help.Header") == BASE_STRINGS["Help.Header"]


def test_cat_theme_changes_the_voice_but_not_the_facts():
    with _themed("cat") as applied:
        assert applied is True
        assert i18n.t("reelay.Help.Header") != BASE_STRINGS["Help.Header"]
        # Keys the overlay doesn't mention keep the plain wording...
        assert i18n.t("reelay.Onboarding.JoinUsage") == BASE_STRINGS["Onboarding.JoinUsage"]
        # ...and a themed one still carries its placeholder and its command.
        assert "Dune" in i18n.t("reelay.Overseerr.Requested", title="Dune")
        assert "/linkme" in i18n.t("reelay.Overseerr.NotLinked")


def test_an_unknown_theme_falls_back_to_plain_copy_instead_of_failing():
    with pytest.warns(UserWarning):
        with _themed("dogs") as applied:
            assert applied is False
            assert i18n.t("reelay.Help.Header") == BASE_STRINGS["Help.Header"]


# --- Variant lines --------------------------------------------------------------

CAT_STRINGS = dict(OVERLAY_STRINGS).get("cat", {})


def test_the_variant_layer_leaves_ordinary_strings_alone():
    """Everything the rest of the suite relies on -- plain strings, plurals,
    the key-as-fallback for a missing key -- has to behave as python-i18n
    always did, whether or not a theme is loaded."""
    assert i18n.t("reelay.Onboarding.JoinUsage") == BASE_STRINGS["Onboarding.JoinUsage"]
    assert i18n.t("reelay.searchresults", count=0) == BASE_STRINGS["searchresults.zero"]
    assert i18n.t("reelay.searchresults", count=7) == "7 search results"
    assert i18n.t("reelay.NoSuchKeyAtAll") == "reelay.NoSuchKeyAtAll"


def test_a_varied_key_uses_all_its_lines_and_never_repeats_back_to_back():
    key, expected = "reelay.messages.AddSuccess", set(CAT_STRINGS["messages.AddSuccess"])
    assert len(expected) > 1, "this test needs a key with variants to be worth running"
    with _themed("cat"):
        seen = [i18n.t(key, subjectWithArticle="The movie") for _ in range(60)]
    rendered = {line.replace("The movie", "%{subjectWithArticle}") for line in seen}
    assert rendered == expected                       # every line gets used
    assert all(a != b for a, b in zip(seen, seen[1:]))  # and never twice running


def test_every_variant_of_a_key_still_fills_in_its_placeholders():
    with _themed("cat"):
        lines = {i18n.t("reelay.Overseerr.Requested", title="Dune") for _ in range(40)}
    assert len(lines) == len(CAT_STRINGS["Overseerr.Requested"])
    assert all("Dune" in line and "%{" not in line for line in lines)


def test_a_varied_plural_form_is_still_pluralised():
    with _themed("cat"):
        zero = {i18n.t("reelay.searchresults", count=0) for _ in range(30)}
        assert zero == set(CAT_STRINGS["searchresults.zero"])
        assert i18n.t("reelay.searchresults", count=4) == "4 search results"


def test_variant_picking_is_seedable_so_a_caller_can_reproduce_a_message():
    lines = ["a", "b", "c"]
    translations.rng.seed(1)
    first = [translations.pickVariant("k", lines) for _ in range(10)]
    translations.rng.seed(1)
    translations._lastPicked.pop("k", None)
    assert [translations.pickVariant("k", lines) for _ in range(10)] == first


def test_a_single_line_key_is_not_treated_as_a_choice():
    assert translations.pickVariant("solo", ["only one"]) == "only one"


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
