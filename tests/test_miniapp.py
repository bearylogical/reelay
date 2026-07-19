import asyncio
import hashlib
import hmac
import json
import time
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


def test_plex_link_flow():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "9", "z", status="approved")
    db.approveMembership("-100111", "9", approved_by="x")
    miniapp._pending_pins.clear()

    async def check(c):
        with patch("reelay.miniapp.plex.createPin", return_value={"id": 111, "code": "XYZ"}), \
             patch("reelay.miniapp.plex.authUrl", return_value="https://plex.tv/auth"):
            r = await c.post("/api/plex/start", headers={"X-Telegram-Init-Data": init_for(9, "z")})
            d = await r.json()
            assert r.status == 200 and d["pinId"] == 111 and d["authUrl"] == "https://plex.tv/auth"

        with patch("reelay.miniapp.plex.pollPin", return_value=None):
            r = await c.get("/api/plex/poll?pinId=111", headers={"X-Telegram-Init-Data": init_for(9, "z")})
            d = await r.json()
            assert d["status"] == "pending"

        with patch("reelay.miniapp.plex.pollPin", return_value="plextoken"), \
             patch("reelay.miniapp.overseerr.signInWithPlex",
                   return_value=({"id": 77, "displayName": "zed", "email": "z@x.com"}, "cookie123")):
            r = await c.get("/api/plex/poll?pinId=111", headers={"X-Telegram-Init-Data": init_for(9, "z")})
            d = await r.json()
            assert r.status == 200 and d["status"] == "linked" and d["displayName"] == "zed"

        link = db.getSeerrLink("-100111", "9")
        assert link["seerr_user_id"] == 77 and link["mode"] == "normal" and link["session_cookie"] == "cookie123"

        # pin was consumed -> polling again reports expired
        r = await c.get("/api/plex/poll?pinId=111", headers={"X-Telegram-Init-Data": init_for(9, "z")})
        d = await r.json()
        assert d["status"] == "expired"
    run_client(check)


def test_plex_poll_rejects_other_users_pin():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "9", "z", status="approved")
    db.approveMembership("-100111", "9", approved_by="x")
    db.upsertMembership("-100111", "10", "y", status="approved")
    db.approveMembership("-100111", "10", approved_by="x")
    miniapp._pending_pins.clear()
    miniapp._pending_pins[222] = {"user_id": "9", "scope_chat_id": "-100111", "created_at": time.time()}

    async def check(c):
        r = await c.get("/api/plex/poll?pinId=222", headers={"X-Telegram-Init-Data": init_for(10, "y")})
        d = await r.json()
        assert d["status"] == "expired"
    run_client(check)


def test_admin_members_requires_admin_role():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "m", status="approved")
    db.approveMembership("-100111", "1", approved_by="x")

    async def check(c):
        r = await c.get("/api/admin/members", headers={"X-Telegram-Init-Data": init_for(1, "m")})
        assert r.status == 403
    run_client(check)


def test_admin_members_lists_pending_and_members():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "admin1", role="admin", status="approved")
    db.approveMembership("-100111", "1", approved_by="x", role="admin")
    db.upsertMembership("-100111", "2", "pending1", status="pending")
    db.linkSeerr("-100111", "1", 55, seerr_username="plexadmin")

    async def check(c):
        r = await c.get("/api/admin/members", headers={"X-Telegram-Init-Data": init_for(1, "admin1")})
        d = await r.json()
        assert r.status == 200
        assert d["invite"]["code"]
        assert len(d["pending"]) == 1 and d["pending"][0]["userId"] == "2"
        assert len(d["members"]) == 1 and d["members"][0]["linked"] is True
        assert d["members"][0]["seerrUsername"] == "plexadmin"
    run_client(check)


def test_admin_approve_and_deny():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "admin1", role="admin", status="approved")
    db.approveMembership("-100111", "1", approved_by="x", role="admin")
    db.upsertMembership("-100111", "2", "pending1", status="pending")
    db.upsertMembership("-100111", "3", "pending2", status="pending")

    async def check(c):
        with patch("reelay.onboarding.askReminderThresholdFor", new=AsyncMock()), \
             patch("reelay.onboarding.sendSeerrPicker", new=AsyncMock()):
            r = await c.post("/api/admin/members/2/approve", headers={"X-Telegram-Init-Data": init_for(1, "admin1")})
            assert r.status == 200
        assert db.getMembership("-100111", "2")["status"] == "approved"

        r = await c.post("/api/admin/members/3/deny", headers={"X-Telegram-Init-Data": init_for(1, "admin1")})
        assert r.status == 200
        assert db.getMembership("-100111", "3")["status"] == "denied"
    run_client(check)


def test_admin_set_role_and_last_admin_guard():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "1", "admin1", role="admin", status="approved")
    db.approveMembership("-100111", "1", approved_by="x", role="admin")
    db.upsertMembership("-100111", "2", "m", status="approved")
    db.approveMembership("-100111", "2", approved_by="x")

    async def check(c):
        r = await c.post(
            "/api/admin/members/2/role",
            headers={"X-Telegram-Init-Data": init_for(1, "admin1"), "Content-Type": "application/json"},
            data=json.dumps({"role": "editor"}),
        )
        assert r.status == 200
        assert db.getMembership("-100111", "2")["role"] == "editor"

        # sole admin can't demote themselves -- would lock the household out
        r = await c.post(
            "/api/admin/members/1/role",
            headers={"X-Telegram-Init-Data": init_for(1, "admin1"), "Content-Type": "application/json"},
            data=json.dumps({"role": "member"}),
        )
        d = await r.json()
        assert r.status == 400 and d["error"] == "last_admin"
    run_client(check)
