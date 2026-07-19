import asyncio
import hashlib
import hmac
import json
import urllib.parse
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import reelay.db as db
import reelay.miniapp as miniapp

TOKEN = "testtoken"


def init_for(uid, uname="u"):
    p = {"user": json.dumps({"id": uid, "username": uname}), "auth_date": "1"}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(p.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    p["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(p)


def run_client(coro):
    async def _run():
        app = miniapp.build_app(AsyncMock())
        async with TestClient(TestServer(app)) as c:
            await coro(c)
    asyncio.run(_run())


def test_verify_init_data():
    good = init_for(1)
    assert miniapp.verify_telegram_init_data(good, TOKEN) is True
    assert miniapp.verify_telegram_init_data(good, "wrong") is False
    # flip the last hash character -> signature no longer matches
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert miniapp.verify_telegram_init_data(tampered, TOKEN) is False


def test_bootstrap_requires_auth():
    async def check(c):
        r = await c.get("/api/bootstrap")
        assert r.status == 401
    run_client(check)


def test_bootstrap_member_sees_only_own():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "a", status="approved")
    db.approveMembership("-100111", "1", approved_by="x")
    db.linkSeerr("-100111", "1", 11)

    async def check(c):
        with patch("reelay.overseerr.getRequestCount", return_value={}), \
             patch("reelay.overseerr.getRequests", return_value=[]) as gr:
            r = await c.get("/api/bootstrap", headers={"X-Telegram-Init-Data": init_for(1, "a")})
            d = await r.json()
            assert r.status == 200 and d["role"] == "member" and d["canSeeQueue"] is False
            assert gr.call_args.kwargs.get("requested_by") == 11
    run_client(check)


def test_queue_editor_only():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "m", status="approved")
    db.approveMembership("-100111", "1", approved_by="x")
    db.upsertMembership("-100111", "2", "e", role="editor", status="approved")
    db.approveMembership("-100111", "2", approved_by="x", role="editor")

    async def check(c):
        r = await c.get("/api/queue", headers={"X-Telegram-Init-Data": init_for(1, "m")})
        assert r.status == 403  # member blocked
        with patch("reelay.radarr.getQueue", return_value=[{"title": "Dune", "mediaType": "movie",
                   "progress": 42, "timeleft": "", "status": "downloading"}]), \
             patch("reelay.sonarr.getQueue", return_value=[]):
            r = await c.get("/api/queue", headers={"X-Telegram-Init-Data": init_for(2, "e")})
            q = await r.json()
            assert r.status == 200 and q[0]["progress"] == 42
    run_client(check)


def test_request_not_linked_returns_409():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "3", "c", status="approved")
    db.approveMembership("-100111", "3", approved_by="x")

    async def check(c):
        r = await c.post("/api/request",
                         headers={"X-Telegram-Init-Data": init_for(3, "c"), "Content-Type": "application/json"},
                         data=json.dumps({"mediaType": "movie", "mediaId": 1}))
        d = await r.json()
        assert r.status == 409 and d["error"] == "not_linked"
    run_client(check)
