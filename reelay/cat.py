"""/cat -- a cat picture or a cat fact, whichever the coin lands on.

Two unrelated public APIs, no key and nothing to configure: cataas.com for the
picture, catfact.ninja for the fact. Neither is anything the bot depends on, so
a service being down never surfaces as an error the user has to think about --
it just means they get the other kind of cat.
"""

import asyncio
import logging
import random

import requests
from telegram.constants import ChatAction

from .commons import checkAllowed
from .config import config
from .translations import i18n
from . import logger

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.cat", logLevel, config.get("logToConsole", False))

PHOTO_URL = "https://cataas.com/cat"
FACT_URL = "https://catfact.ninja/fact"

# Short on purpose: nobody waits on a joke command, and both calls hold a
# worker thread while they're in flight.
TIMEOUT = 8
# Telegram rejects a photo upload over 10 MB. A cat that big is a fact instead.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
# catfact.ninja is free to return an essay; the bot isn't.
MAX_FACT_CHARS = 400

# Seedable so a test can pin the coin flip; the bot never seeds it.
rng = random.Random()


def fetchPhoto():
    """A random cat as JPEG bytes, or None if cataas.com is unhelpful.

    Downloaded here rather than passed to Telegram as a URL: Telegram caches
    an uploaded URL by that URL, so `send_photo(photo=PHOTO_URL)` would serve
    everyone the same cat forever."""
    try:
        resp = requests.get(PHOTO_URL, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Could not fetch a cat picture: {e}")
        return None
    if not resp.headers.get("Content-Type", "").startswith("image/"):
        logger.warning("cataas.com returned something that isn't an image.")
        return None
    if not resp.content or len(resp.content) > MAX_PHOTO_BYTES:
        logger.warning(f"Cat picture is an unusable size ({len(resp.content)} bytes).")
        return None
    return resp.content


def fetchFact():
    """One cat fact as a string, or None if catfact.ninja is unhelpful."""
    try:
        resp = requests.get(FACT_URL, timeout=TIMEOUT)
        resp.raise_for_status()
        fact = str(resp.json()["fact"]).strip()
    except Exception as e:
        logger.warning(f"Could not fetch a cat fact: {e}")
        return None
    if not fact or len(fact) > MAX_FACT_CHARS:
        logger.warning("catfact.ninja returned nothing usable.")
        return None
    return fact


async def sendPhoto(update, context):
    """True if a cat picture made it to the chat."""
    chatId = update.effective_message.chat_id
    await context.bot.send_chat_action(chat_id=chatId, action=ChatAction.UPLOAD_PHOTO)
    # Blocking `requests` against a third party that owes us nothing -- off the
    # event loop, or one slow cat stalls every other chat's handlers too.
    photo = await asyncio.to_thread(fetchPhoto)
    if photo is None:
        return False
    await context.bot.send_photo(
        chat_id=chatId, photo=photo, caption=i18n.t("reelay.Cat.PhotoCaption")
    )
    return True


async def sendFact(update, context):
    """True if a cat fact made it to the chat."""
    chatId = update.effective_message.chat_id
    await context.bot.send_chat_action(chat_id=chatId, action=ChatAction.TYPING)
    fact = await asyncio.to_thread(fetchFact)
    if fact is None:
        return False
    await context.bot.send_message(chat_id=chatId, text=i18n.t("reelay.Cat.Fact", fact=fact))
    return True


async def cat(update, context):
    """/cat -- a random cat picture or a random cat fact."""
    if config.get("enableAllowlist") and not checkAllowed(update, "regular"):
        # When using this mode, bot will remain silent if user is not in the allowlist.txt
        logger.info("Allowlist is enabled, but userID isn't added into 'allowlist.txt'. So bot stays silent")
        return

    # Shuffled rather than picked: the first is the coin flip, and the second is
    # what the caller gets when that service is down -- a cat either way.
    senders = [sendPhoto, sendFact]
    rng.shuffle(senders)
    for send in senders:
        if await send(update, context):
            return

    await context.bot.send_message(
        chat_id=update.effective_message.chat_id, text=i18n.t("reelay.Cat.Unavailable")
    )
