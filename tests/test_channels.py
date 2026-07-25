import asyncio
import types
from unittest.mock import AsyncMock

import reelay.channels as channels
import reelay.db as db

SCOPE = "-100111"
POSTER = "https://image.tmdb.org/t/p/w500/dune.jpg"


def _ctx():
    return types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))


def _announce(ctx, **kw):
    return asyncio.run(channels.announce(ctx, SCOPE, channels.CATEGORY_REQUESTS, "🎬 a requested *Dune*", **kw))


def test_announce_without_poster_sends_plain_text():
    db.upsertScope(SCOPE, title="Fam")
    db.setChannelRoute(SCOPE, "requests", SCOPE, "7")
    ctx = _ctx()
    assert _announce(ctx) is True
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["text"] == "🎬 a requested *Dune*"
    assert kwargs["chat_id"] == int(SCOPE) and kwargs["message_thread_id"] == 7
    assert "link_preview_options" not in kwargs


def test_announce_with_poster_attaches_it_as_the_link_preview():
    db.upsertScope(SCOPE, title="Fam")
    db.setChannelRoute(SCOPE, "requests", SCOPE, "7")
    ctx = _ctx()
    assert _announce(ctx, photo=POSTER) is True
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["text"] == "🎬 a requested *Dune*"
    lpo = kwargs["link_preview_options"]
    assert lpo.url == POSTER and lpo.prefer_large_media is True and lpo.show_above_text is True


def test_announce_with_poster_still_no_ops_without_a_route():
    ctx = _ctx()
    assert _announce(ctx, photo=POSTER) is False
    ctx.bot.send_message.assert_not_called()
