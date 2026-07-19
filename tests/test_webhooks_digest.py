import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import reelay.db as db
import reelay.digest as digest
import reelay.miniapp as miniapp

SCOPE = "-1001111111111"


def post_webhooks(payloads):
    result = {"status": [], "bot": None}

    async def _run():
        bot = AsyncMock()
        app = miniapp.build_app(bot)
        async with TestClient(TestServer(app)) as c:
            for p in payloads:
                r = await c.post("/overseerr/webhook/s3cr3t",
                                 headers={"Content-Type": "application/json"}, data=json.dumps(p))
                result["status"].append(r.status)
            result["bot"] = bot
    asyncio.run(_run())
    return result


def test_webhook_records_available_only_no_adhoc_post():
    db.upsertScope(SCOPE, title="Fam")
    res = post_webhooks([
        {"notification_type": "MEDIA_AVAILABLE", "subject": "The Matrix",
         "media": {"media_type": "movie"}, "request": {"requestedBy_username": "bob", "requestedBy_email": "b@x.com"}},
        {"notification_type": "MEDIA_PENDING", "subject": "Ignored"},
    ])
    assert res["status"] == [200, 200]
    res["bot"].send_message.assert_not_called()  # no ad-hoc posting
    events = db.getRecentMediaEvents(7)
    assert len(events) == 1 and events[0]["title"] == "The Matrix"


def test_webhook_wrong_secret_is_404():
    async def _run():
        app = miniapp.build_app(AsyncMock())
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/overseerr/webhook/nope", data="{}")
            assert r.status == 404
    asyncio.run(_run())


def test_weekly_digest_group_and_personal():
    db.upsertScope(SCOPE, title="Fam")
    db.setChannelRoute(SCOPE, "updates", SCOPE, "70")
    db.upsertMembership(SCOPE, "1", "alice", status="approved")
    db.approveMembership(SCOPE, "1", approved_by="x")
    db.linkSeerr(SCOPE, "1", 11, seerr_username="alice", seerr_email="a@x.com")
    db.recordMediaEvent("The Matrix", "movie", "alice", "a@x.com")
    db.recordMediaEvent("Dune", "movie", "bob", "b@x.com")

    ctx = type("C", (), {"bot": AsyncMock()})()
    scope = db.getScope(SCOPE)
    asyncio.run(digest.send_weekly_digest_to_scope(ctx, scope, db.getRecentMediaEvents(7)))

    group = [c for c in ctx.bot.send_message.call_args_list if c.kwargs.get("message_thread_id") == 70]
    dm = [c for c in ctx.bot.send_message.call_args_list if "message_thread_id" not in c.kwargs]
    assert group and "The Matrix" in group[0].kwargs["text"] and "Dune" in group[0].kwargs["text"]
    # alice's personal DM only has her own request
    assert any(c.kwargs["chat_id"] == 1 and "The Matrix" in c.kwargs["text"]
               and "Dune" not in c.kwargs["text"] for c in dm)


def test_weekly_breakdown_splits_by_media_type():
    db.recordMediaEvent("The Matrix", "movie")
    db.recordMediaEvent("The Matrix", "movie")  # duplicate, deduped
    db.recordMediaEvent("Dune", "movie")
    db.recordMediaEvent("Severance", "tv")

    result = digest.weekly_breakdown(db.getRecentMediaEvents(7))
    assert result["counts"] == {"movie": 2, "tv": 1}
    assert set(result["movies"]) == {"The Matrix", "Dune"}
    assert result["tv"] == ["Severance"]


def test_weekly_digest_tick_only_sends_to_due_scopes():
    db.upsertScope(SCOPE, title="Fam")
    db.setChannelRoute(SCOPE, "updates", SCOPE, "70")
    db.recordMediaEvent("The Matrix", "movie")

    other = "-1002222222222"
    db.upsertScope(other, title="Other")
    db.setChannelRoute(other, "updates", other, "80")

    # SCOPE is due right now; `other` stays disabled (default) and must not post.
    now = datetime.now()
    db.setWeeklyDigestConfig(SCOPE, enabled=True, day=now.strftime("%A").lower(), hour=now.hour)

    ctx = type("C", (), {"bot": AsyncMock()})()
    asyncio.run(digest.weekly_digest_tick(ctx))

    posted_threads = {c.kwargs.get("message_thread_id") for c in ctx.bot.send_message.call_args_list}
    assert 70 in posted_threads
    assert 80 not in posted_threads
    assert db.getScope(SCOPE)["weekly_digest_last_sent"] == now.date().isoformat()

    # Running the tick again the same hour must not double-post (last_sent guard).
    ctx2 = type("C", (), {"bot": AsyncMock()})()
    asyncio.run(digest.weekly_digest_tick(ctx2))
    ctx2.bot.send_message.assert_not_called()
