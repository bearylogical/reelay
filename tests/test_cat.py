"""Tests for /cat.

Both services it calls are third parties with no uptime promise, so most of
what matters here is what happens when one of them doesn't answer: the caller
still gets a cat, and the bot never posts a broken one.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import reelay.cat as cat


class _Resp:
    """Just enough of requests.Response for the two calls cat.py makes."""

    def __init__(self, *, content=b"", contentType="image/jpeg", json=None, status=200):
        self.content = content
        self.headers = {"Content-Type": contentType}
        self.status_code = status
        self._json = json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


def _responder(photo=None, fact=None):
    """A fake requests.get that answers each service with what it's given --
    an exception class/instance for a service that's down."""
    def get(url, **kwargs):
        answer = photo if url == cat.PHOTO_URL else fact
        if isinstance(answer, Exception):
            raise answer
        return answer
    return get


PHOTO_OK = _Resp(content=b"\xff\xd8jpegbytes")
FACT_OK = _Resp(contentType="application/json", json={"fact": "Cats sleep 70% of their lives."})


def _update(chatId=42):
    update = MagicMock()
    update.effective_message.chat_id = chatId
    return update


def _context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.send_photo = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context


def _pin(monkeypatch, first):
    """Pin the coin flip so a test knows which service is tried first."""
    orders = {"photo": lambda seq: None, "fact": lambda seq: seq.reverse()}
    monkeypatch.setattr(cat.rng, "shuffle", orders[first])


def _run(monkeypatch, *, first, photo=None, fact=None):
    monkeypatch.setattr(cat.requests, "get", _responder(photo, fact))
    _pin(monkeypatch, first)
    update, context = _update(), _context()
    asyncio.run(cat.cat(update, context))
    return context


# --- Fetching -------------------------------------------------------------------

def test_a_photo_comes_back_as_bytes(monkeypatch):
    monkeypatch.setattr(cat.requests, "get", _responder(photo=PHOTO_OK))
    assert cat.fetchPhoto() == PHOTO_OK.content


def test_a_photo_that_isnt_an_image_is_refused(monkeypatch):
    """cataas.com answering with an HTML error page must not be posted as a cat."""
    monkeypatch.setattr(cat.requests, "get", _responder(
        photo=_Resp(content=b"<html>down for maintenance</html>", contentType="text/html")))
    assert cat.fetchPhoto() is None


def test_a_photo_too_big_for_telegram_is_refused(monkeypatch):
    """Telegram would reject the upload; better to fall through to a fact."""
    monkeypatch.setattr(cat.requests, "get", _responder(
        photo=_Resp(content=b"x" * (cat.MAX_PHOTO_BYTES + 1))))
    assert cat.fetchPhoto() is None


@pytest.mark.parametrize("answer", [
    ConnectionError("no route to cat"),
    _Resp(status=503),
    _Resp(contentType="text/html", json=None),
])
def test_a_fact_that_doesnt_arrive_is_none_not_an_exception(monkeypatch, answer):
    monkeypatch.setattr(cat.requests, "get", _responder(fact=answer))
    assert cat.fetchFact() is None


def test_a_fact_comes_back_as_text(monkeypatch):
    monkeypatch.setattr(cat.requests, "get", _responder(fact=FACT_OK))
    assert cat.fetchFact() == "Cats sleep 70% of their lives."


def test_an_essay_is_not_a_fact(monkeypatch):
    monkeypatch.setattr(cat.requests, "get", _responder(
        fact=_Resp(contentType="application/json", json={"fact": "meow " * 200})))
    assert cat.fetchFact() is None


# --- The command ----------------------------------------------------------------

def test_the_photo_side_of_the_coin_sends_a_photo(monkeypatch):
    context = _run(monkeypatch, first="photo", photo=PHOTO_OK, fact=FACT_OK)
    assert context.bot.send_photo.call_args.kwargs["photo"] == PHOTO_OK.content
    context.bot.send_message.assert_not_called()


def test_the_fact_side_of_the_coin_sends_the_fact_text(monkeypatch):
    context = _run(monkeypatch, first="fact", photo=PHOTO_OK, fact=FACT_OK)
    assert "Cats sleep 70% of their lives." in context.bot.send_message.call_args.kwargs["text"]
    context.bot.send_photo.assert_not_called()


def test_a_dead_photo_service_still_gets_you_a_cat(monkeypatch):
    context = _run(monkeypatch, first="photo", photo=ConnectionError("down"), fact=FACT_OK)
    context.bot.send_photo.assert_not_called()
    assert "Cats sleep 70% of their lives." in context.bot.send_message.call_args.kwargs["text"]


def test_a_dead_fact_service_still_gets_you_a_cat(monkeypatch):
    context = _run(monkeypatch, first="fact", photo=PHOTO_OK, fact=ConnectionError("down"))
    assert context.bot.send_photo.call_args.kwargs["photo"] == PHOTO_OK.content


def test_both_services_down_says_so_once(monkeypatch):
    context = _run(monkeypatch, first="photo",
                   photo=ConnectionError("down"), fact=ConnectionError("down"))
    context.bot.send_photo.assert_not_called()
    context.bot.send_message.assert_called_once()
    assert "/cat" in context.bot.send_message.call_args.kwargs["text"]


def test_the_coin_actually_lands_on_both_sides(monkeypatch):
    """Without the shuffle pinned, repeated calls have to produce both kinds --
    a coin that always comes up photo isn't the feature that was asked for."""
    monkeypatch.setattr(cat.requests, "get", _responder(photo=PHOTO_OK, fact=FACT_OK))
    cat.rng.seed(0)
    context = _context()
    for _ in range(30):
        asyncio.run(cat.cat(_update(), context))
    assert context.bot.send_photo.called and context.bot.send_message.called


def test_an_allowlisted_bot_stays_silent_for_strangers(monkeypatch):
    """Same rule as every other command: allowlist on and you're not on it
    means no reply at all, not an error."""
    monkeypatch.setitem(cat.config, "enableAllowlist", True)
    context = _run(monkeypatch, first="photo", photo=PHOTO_OK, fact=FACT_OK)
    context.bot.send_photo.assert_not_called()
    context.bot.send_message.assert_not_called()
