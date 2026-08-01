"""Loads the bot's copy: an optional personality theme on top of the neutral
wording, and variant lines picked at random so a bot you talk to every day
doesn't repeat itself word for word.

A theme is *not* a language: it's an overlay of the same keys in a different
voice, living in translations/themes/<name>/reelay.<locale>.yml. Appending its
directory after the neutral one is all it takes -- python-i18n loads every
load_path directory that holds the namespace file, in order, and later loads
win. So an overlay only has to list the strings it actually changes; every key
it leaves out keeps the plain wording, and a theme that has no file for the
active language simply doesn't apply.

Variants are a YAML list where a string would be, and any translation file may
use them:

    End:
      - "That's a wrap — I'm off for a nap. 😽"
      - "Done. I'll be in the sunbeam if you need me."

python-i18n has no idea what to do with that (its formatter wants a string), so
`i18n` exported from here is the library with one thing added: t() resolves a
list to one of its lines first. Everything else -- locales, fallback, plurals,
placeholder syntax -- is still python-i18n's, and a key whose value is a plain
string takes exactly the path it always did.
"""

import os
import random
import warnings

import i18n as _i18n
from i18n import resource_loader as _loader
from i18n import translations as _store
from i18n import translator as _translator

from .config import config
from .definitions import LANG_PATH, THEMES, THEMES_PATH

_i18n.load_path.append(LANG_PATH)
_i18n.set('locale', config["language"])
_i18n.set('fallback', 'en-us')


# --- Themes -------------------------------------------------------------------

def themeDir(name):
    """The overlay directory for a theme, or None for the plain wording."""
    name = str(name or "default").strip().lower()
    if name in ("", "default", "none"):
        return None
    return os.path.join(THEMES_PATH, name)


def applyTheme(name):
    """Put a theme's wording in front of the neutral copy. Unknown or
    half-installed themes are a cosmetic problem, never a fatal one: warn and
    leave the bot speaking plainly. (checkConfigValues() is what actually tells
    the admin they typo'd it.)"""
    directory = themeDir(name)
    if directory is None:
        return False
    if str(name).strip().lower() not in THEMES:
        warnings.warn(f"Unknown theme {name!r} — using the default wording. Known themes: {', '.join(THEMES)}.")
        return False
    if not os.path.isdir(directory):
        warnings.warn(f"Theme {name!r} has no directory at {directory} — using the default wording.")
        return False
    if directory not in _i18n.load_path:
        _i18n.load_path.append(directory)
    return True


# --- Variants -----------------------------------------------------------------

# Seedable so a test can pin the choice; the bot never seeds it.
rng = random.Random()

# The line each key produced last time, so the next pick can avoid it. Two
# lines in a row being identical is the one thing that makes a varied bot look
# broken rather than varied, and with two or three variants plain random
# choice does it constantly.
_lastPicked = {}


def pickVariant(key, variants):
    """One line out of a variant list, never the one this key used last."""
    if len(variants) == 1:
        return variants[0]
    fresh = [v for v in variants if v != _lastPicked.get(key)] or list(variants)
    chosen = rng.choice(fresh)
    _lastPicked[key] = chosen
    return chosen


def _stored(key, locale):
    """The raw value behind a key -- string, plural dict or variant list --
    without formatting it. Mirrors what python-i18n's own t() does to find a
    translation: look in the loaded container, and if it isn't there yet, go
    and load the file that would hold it."""
    if not _store.has(key, locale):
        _loader.search_translation(key, locale)
    return _store.get(key, locale) if _store.has(key, locale) else None


def t(key, **kwargs):
    """python-i18n's t(), with variant lists resolved to a single line.

    Anything that isn't a list -- including a key that doesn't resolve at all
    -- is handed straight to the library, so fallback locales, missing-key
    behaviour and plural rules stay exactly as they were.
    """
    locale = kwargs.pop("locale", _i18n.config.get("locale"))
    for candidate in (locale, _i18n.config.get("fallback")):
        raw = _stored(key, candidate)
        if raw is None:
            continue
        if isinstance(raw, dict) and "count" in kwargs:
            raw = _translator.pluralize(key, raw, kwargs["count"])
        if isinstance(raw, list) and raw:
            line = pickVariant(key, raw)
            return _translator.TranslationFormatter(line).format(**kwargs)
        break
    return _i18n.t(key, locale=locale, **kwargs)


class _I18n:
    """python-i18n as the rest of the bot sees it: the library, plus a t() that
    understands variant lists. Everything else passes through untouched, so
    `i18n.load_path`, `i18n.set` and friends are the real thing."""

    t = staticmethod(t)

    def __getattr__(self, name):
        return getattr(_i18n, name)


i18n = _I18n()


applyTheme(config.get("theme"))
