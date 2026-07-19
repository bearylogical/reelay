import asyncio
import json
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
    asyncio.run(digest.send_weekly_digest(ctx))

    group = [c for c in ctx.bot.send_message.call_args_list if c.kwargs.get("message_thread_id") == 70]
    dm = [c for c in ctx.bot.send_message.call_args_list if "message_thread_id" not in c.kwargs]
    assert group and "The Matrix" in group[0].kwargs["text"] and "Dune" in group[0].kwargs["text"]
    # alice's personal DM only has her own request
    assert any(c.kwargs["chat_id"] == 1 and "The Matrix" in c.kwargs["text"]
               and "Dune" not in c.kwargs["text"] for c in dm)
