import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import reelay.config as cfg
import reelay.db as db
import reelay.digest as digest
import reelay.miniapp as miniapp

SCOPE = "-1001111111111"


def post_to(path, payloads):
    result = {"status": [], "bot": None}

    async def _run():
        bot = AsyncMock()
        app = miniapp.build_app(bot)
        async with TestClient(TestServer(app)) as c:
            for p in payloads:
                r = await c.post(path, headers={"Content-Type": "application/json"}, data=json.dumps(p))
                result["status"].append(r.status)
            result["bot"] = bot
    asyncio.run(_run())
    return result


def post_webhooks(payloads):
    return post_to("/overseerr/webhook/s3cr3t", payloads)


def send_digest():
    """Run the group digest for SCOPE and return (bot, group_text, dm_calls)."""
    ctx = type("C", (), {"bot": AsyncMock()})()
    delivered = asyncio.run(
        digest.send_weekly_digest_to_scope(ctx, db.getScope(SCOPE), db.getRecentMediaEvents(7))
    )
    calls = ctx.bot.send_message.call_args_list
    group = [c for c in calls if c.kwargs.get("message_thread_id") == 70]
    dms = [c for c in calls if "message_thread_id" not in c.kwargs]
    return delivered, (group[0].kwargs["text"] if group else None), dms


def routed_scope():
    db.upsertScope(SCOPE, title="Fam")
    db.setChannelRoute(SCOPE, "updates", SCOPE, "70")


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
    assert result["counts"] == {"movie": 2, "tv": 1, "other": 0}
    assert {e["title"] for e in result["movies"]} == {"The Matrix", "Dune"}
    assert [e["title"] for e in result["tv"]] == ["Severance"]


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


# --- Radarr / Sonarr webhooks -------------------------------------------------

RADARR = "/radarr/webhook/r4d4rr"
SONARR = "/sonarr/webhook/s0n4rr"


def radarr_payload(event_type, title="The Matrix", tmdb=603, **extra):
    return {"eventType": event_type, "movie": {"title": title, "year": 1999, "tmdbId": tmdb}, **extra}


def sonarr_payload(event_type, title="Severance", tvdb=371980, **extra):
    return {"eventType": event_type, "series": {"title": title, "tvdbId": tvdb},
            "episodes": [{"episodeNumber": 1}], **extra}


def test_radarr_import_records_available_movie():
    res = post_to(RADARR, [radarr_payload("Download", isUpgrade=False)])
    assert res["status"] == [200]
    res["bot"].send_message.assert_not_called()  # recorded, not announced
    (e,) = db.getRecentMediaEvents(7)
    assert e["title"] == "The Matrix"
    assert e["media_type"] == "movie"
    assert e["source"] == "radarr"
    assert e["event"] == digest.EVENT_AVAILABLE
    assert e["external_id"] == "tmdb:603"


def test_sonarr_import_records_available_tv():
    res = post_to(SONARR, [sonarr_payload("Download", isUpgrade=False)])
    assert res["status"] == [200]
    (e,) = db.getRecentMediaEvents(7)
    assert (e["media_type"], e["source"], e["external_id"]) == ("tv", "sonarr", "tvdb:371980")
    assert e["event"] == digest.EVENT_AVAILABLE


def test_arr_quality_upgrade_is_not_new_content():
    res = post_to(RADARR, [radarr_payload("Download", isUpgrade=True)])
    assert res["status"] == [200]
    assert db.getRecentMediaEvents(7) == []


def test_arr_add_events_record_as_added_not_available():
    post_to(RADARR, [radarr_payload("MovieAdded")])
    post_to(SONARR, [sonarr_payload("SeriesAdd")])
    assert {e["event"] for e in db.getRecentMediaEvents(7)} == {digest.EVENT_ADDED}


def test_arr_unknown_event_is_ignored():
    res = post_to(RADARR, [radarr_payload("Grab"), radarr_payload("Rename"),
                           {"eventType": "Health", "level": "warning"}])
    assert res["status"] == [200, 200, 200]
    assert db.getRecentMediaEvents(7) == []


def test_arr_wrong_secret_is_404():
    res = post_to("/radarr/webhook/nope", [radarr_payload("Download")])
    assert res["status"] == [404]
    assert db.getRecentMediaEvents(7) == []


def test_arr_payload_without_title_is_rejected_not_recorded():
    # An untitled row would dedupe against every other untitled row.
    res = post_to(RADARR, [{"eventType": "Download", "movie": {"tmdbId": 603}}])
    assert res["status"] == [400]
    assert db.getRecentMediaEvents(7) == []


def test_arr_test_button_announces_into_updates():
    routed_scope()
    res = post_to(RADARR, [{"eventType": "Test"}])
    assert res["status"] == [200]
    (call,) = res["bot"].send_message.call_args_list
    assert call.kwargs["message_thread_id"] == 70
    assert "Radarr/Sonarr" in call.kwargs["text"]
    assert db.getRecentMediaEvents(7) == []


def test_sonarr_per_episode_flood_collapses_to_one_line():
    routed_scope()
    for n in range(1, 11):  # Sonarr fires once per imported episode
        post_to(SONARR, [sonarr_payload("Download", isUpgrade=False)])
    assert len(db.getRecentMediaEvents(7)) == 10  # all stored...
    _, group, _ = send_digest()
    assert group.count("Severance") == 1  # ...but announced once


# --- cross-source dedupe ------------------------------------------------------

def test_same_item_from_overseerr_and_radarr_listed_once_via_external_id():
    routed_scope()
    db.recordMediaEvent("The Matrix (1999)", "movie", source="overseerr", external_id="tmdb:603")
    db.recordMediaEvent("The Matrix", "movie", source="radarr", external_id="tmdb:603")
    _, group, _ = send_digest()
    assert group.count("Matrix") == 1


def test_same_item_listed_once_via_normalized_title_when_ids_differ():
    # Radarr knows tmdb ids, Sonarr knows tvdb ids -- they never match, so the
    # normalized title is the only bridge between the two sources.
    routed_scope()
    db.recordMediaEvent("The Matrix (1999)", "movie", source="overseerr")
    db.recordMediaEvent("the matrix", "movie", source="radarr", external_id="tmdb:603")
    _, group, _ = send_digest()
    assert group.lower().count("matrix") == 1


def test_untyped_event_does_not_double_list_a_typed_one():
    routed_scope()
    db.recordMediaEvent("Dune", None, source="overseerr")
    db.recordMediaEvent("Dune", "movie", source="radarr")
    _, group, _ = send_digest()
    assert group.count("Dune") == 1


def test_same_title_different_media_type_stays_separate():
    # "Fargo" is both a 1996 film and a series -- collapsing them would hide one.
    routed_scope()
    db.recordMediaEvent("Fargo", "movie", source="radarr")
    db.recordMediaEvent("Fargo", "tv", source="sonarr")
    _, group, _ = send_digest()
    assert group.count("Fargo") == 2
    assert "🎬 Fargo" in group and "📺 Fargo" in group


# --- available vs added -------------------------------------------------------

def test_group_post_splits_watchable_from_coming_soon():
    routed_scope()
    db.recordMediaEvent("The Matrix", "movie", source="radarr", event=digest.EVENT_AVAILABLE)
    db.recordMediaEvent("Dune", "movie", source="reelay", event=digest.EVENT_ADDED)
    delivered, group, _ = send_digest()
    assert delivered
    available, coming = group.split("\n\n")
    assert "The Matrix" in available and "Dune" not in available
    assert "Dune" in coming and "coming soon" in coming.lower()


def test_item_added_then_imported_same_week_is_announced_only_as_available():
    routed_scope()
    db.recordMediaEvent("Dune", "movie", source="reelay", event=digest.EVENT_ADDED,
                        external_id="tmdb:438631")
    db.recordMediaEvent("Dune", "movie", source="radarr", event=digest.EVENT_AVAILABLE,
                        external_id="tmdb:438631")
    _, group, _ = send_digest()
    assert group.count("Dune") == 1
    assert "coming soon" not in group.lower()


def test_added_only_week_still_posts():
    routed_scope()
    db.recordMediaEvent("Dune", "movie", source="reelay", event=digest.EVENT_ADDED)
    delivered, group, _ = send_digest()
    assert delivered and "Dune" in group


def test_personal_dm_never_promises_something_still_downloading():
    routed_scope()
    db.upsertMembership(SCOPE, "1", "alice", status="approved")
    db.approveMembership(SCOPE, "1", approved_by="x")
    db.linkSeerr(SCOPE, "1", 11, seerr_username="alice", seerr_email="a@x.com")
    db.recordMediaEvent("The Matrix", "movie", "alice", "a@x.com", event=digest.EVENT_AVAILABLE)
    db.recordMediaEvent("Dune", "movie", "alice", "a@x.com", event=digest.EVENT_ADDED)

    _, _, dms = send_digest()
    (dm,) = [c for c in dms if c.kwargs["chat_id"] == 1]
    assert "The Matrix" in dm.kwargs["text"]
    assert "Dune" not in dm.kwargs["text"]


# --- delivery reporting -------------------------------------------------------

def test_digest_reports_undelivered_when_no_updates_route_is_set():
    db.upsertScope(SCOPE, title="Fam")  # deliberately no /routehere updates
    db.recordMediaEvent("The Matrix", "movie")
    delivered, group, dms = send_digest()
    assert delivered is False
    assert group is None and dms == []


def test_digest_counts_as_delivered_when_only_dms_went_out():
    db.upsertScope(SCOPE, title="Fam")  # no group route, but a linked member
    db.upsertMembership(SCOPE, "1", "alice", status="approved")
    db.approveMembership(SCOPE, "1", approved_by="x")
    db.linkSeerr(SCOPE, "1", 11, seerr_username="alice", seerr_email="a@x.com")
    db.recordMediaEvent("The Matrix", "movie", "alice", "a@x.com")
    delivered, _, dms = send_digest()
    assert delivered is True and len(dms) == 1


def test_empty_week_is_not_delivered():
    routed_scope()
    delivered, group, _ = send_digest()
    assert delivered is False and group is None


def test_tick_marks_sent_even_when_undelivered_so_it_cannot_re_dm():
    # The due window is a single hour; leaving it unmarked would let the next
    # tick inside that hour re-DM every member.
    db.upsertScope(SCOPE, title="Fam")  # no updates route
    db.recordMediaEvent("The Matrix", "movie")
    now = datetime.now()
    db.setWeeklyDigestConfig(SCOPE, enabled=True, day=now.strftime("%A").lower(), hour=now.hour)

    ctx = type("C", (), {"bot": AsyncMock()})()
    asyncio.run(digest.weekly_digest_tick(ctx))
    assert db.getScope(SCOPE)["weekly_digest_last_sent"] == now.date().isoformat()


def test_weekly_breakdown_separates_added_and_untyped():
    db.recordMediaEvent("The Matrix", "movie")
    db.recordMediaEvent("Mystery Item", None)
    db.recordMediaEvent("Dune", "movie", event=digest.EVENT_ADDED)

    result = digest.weekly_breakdown(db.getRecentMediaEvents(7))
    assert result["counts"] == {"movie": 1, "tv": 0, "other": 1}
    assert [e["title"] for e in result["other"]] == ["Mystery Item"]
    assert [e["title"] for e in result["added"]] == ["Dune"]


def test_legacy_rows_predating_the_source_columns_still_digest():
    # Rows written before source/event/external_id existed must read back as
    # Overseerr availability -- that is what every one of them was.
    routed_scope()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO media_events (title, media_type, occurred_at) VALUES (?, ?, datetime('now'))",
            ("The Matrix", "movie"),
        )
    (e,) = db.getRecentMediaEvents(7)
    assert (e["source"], e["event"], e["external_id"]) == ("overseerr", "available", None)
    _, group, _ = send_digest()
    assert "The Matrix" in group and "coming soon" not in group.lower()


def test_unsubstituted_template_id_is_not_treated_as_an_id():
    # Overseerr's payload is user-edited. If '{{media_tmdbid}}' arrives
    # unsubstituted, treating it as an id would give every event the same one
    # and collapse the entire digest into a single line.
    routed_scope()
    for title in ("The Matrix", "Dune", "Arrival"):
        post_webhooks([{
            "notification_type": "MEDIA_AVAILABLE", "subject": title,
            "media": {"media_type": "movie", "tmdbId": "{{media_tmdbid}}"},
        }])
    assert {e["external_id"] for e in db.getRecentMediaEvents(7)} == {None}
    _, group, _ = send_digest()
    for title in ("The Matrix", "Dune", "Arrival"):
        assert title in group


def test_blank_and_zero_ids_are_ignored():
    for bad in ("", "0", 0, None, "n/a"):
        post_to(RADARR, [radarr_payload("Download", title=f"T{bad}", tmdb=bad)])
    assert {e["external_id"] for e in db.getRecentMediaEvents(7)} == {None}


def test_real_ids_still_dedupe_across_sources():
    routed_scope()
    post_webhooks([{"notification_type": "MEDIA_AVAILABLE", "subject": "The Matrix (1999)",
                    "media": {"media_type": "movie", "tmdbId": "603"}}])
    post_to(RADARR, [radarr_payload("Download", title="Matrix, The", tmdb=603)])
    assert len(db.getRecentMediaEvents(7)) == 2
    _, group, _ = send_digest()
    assert len([ln for ln in group.splitlines() if "Matrix" in ln]) == 1


# --- source attribution -------------------------------------------------------

def sources_for(title, breakdown):
    everything = breakdown["movies"] + breakdown["tv"] + breakdown["other"] + breakdown["added"]
    return next(e["sources"] for e in everything if e["title"] == title)


def test_item_reported_by_two_sources_credits_both_not_just_the_first():
    # Dedupe keeps the earliest row; naively reading its `source` would credit
    # only whichever webhook happened to fire first.
    db.recordMediaEvent("The Matrix (1999)", "movie", source="overseerr", external_id="tmdb:603")
    db.recordMediaEvent("Matrix, The", "movie", source="radarr", external_id="tmdb:603")

    b = digest.weekly_breakdown(db.getRecentMediaEvents(7))
    assert b["counts"]["movie"] == 1
    assert sources_for("The Matrix (1999)", b) == ["overseerr", "radarr"]


def test_added_then_available_credits_both_sources_under_available():
    db.recordMediaEvent("Dune", "movie", source="reelay", event=digest.EVENT_ADDED,
                        external_id="tmdb:438631")
    db.recordMediaEvent("Dune", "movie", source="radarr", event=digest.EVENT_AVAILABLE,
                        external_id="tmdb:438631")

    b = digest.weekly_breakdown(db.getRecentMediaEvents(7))
    assert b["added"] == []
    assert sources_for("Dune", b) == ["radarr", "reelay"]


def test_source_counts_tally_items_per_source():
    db.recordMediaEvent("The Matrix", "movie", source="overseerr", external_id="tmdb:603")
    db.recordMediaEvent("The Matrix", "movie", source="radarr", external_id="tmdb:603")
    db.recordMediaEvent("Severance", "tv", source="sonarr")
    db.recordMediaEvent("Dune", "movie", source="reelay", event=digest.EVENT_ADDED)

    b = digest.weekly_breakdown(db.getRecentMediaEvents(7))
    # The Matrix counts for both sources that reported it, so these
    # deliberately total more than the 3 distinct items.
    assert b["sourceCounts"] == {"overseerr": 1, "radarr": 1, "reelay": 1, "sonarr": 1}


def test_episode_flood_credits_sonarr_once_per_item():
    for _ in range(10):
        db.recordMediaEvent("Severance", "tv", source="sonarr", external_id="tvdb:371980")
    b = digest.weekly_breakdown(db.getRecentMediaEvents(7))
    assert b["sourceCounts"] == {"sonarr": 1}
    assert sources_for("Severance", b) == ["sonarr"]


def test_dedupe_does_not_mutate_the_caller_rows():
    # _dedupe returns copies; a caller re-reading the same list must not see
    # `sources` smeared onto its own dicts.
    rows = [{"title": "The Matrix", "media_type": "movie", "source": "radarr"}]
    digest._dedupe(rows)
    assert rows == [{"title": "The Matrix", "media_type": "movie", "source": "radarr"}]


def test_group_post_stays_free_of_source_tags():
    # Sources are an admin diagnostic; the family-facing post must not carry them.
    routed_scope()
    db.recordMediaEvent("The Matrix", "movie", source="radarr")
    _, group, _ = send_digest()
    assert "radarr" not in group.lower()
    assert group.splitlines()[1] == "🎬 The Matrix"


# --- Overseerr poll ------------------------------------------------------------
#
# The push sources above all require a webhook the admin may never have wired.
# These cover the pull path: what Overseerr itself reports, with no webhook.

def seerr_request(tmdb=None, tvdb=None, media_status=5, request_status=2,
                  media_added=None, created=None, updated=None, user=None, req_id=1):
    """One Overseerr /api/v1/request result, trimmed to the fields we read."""
    return {
        "id": req_id,
        "status": request_status,
        "createdAt": created or iso_days_ago(1),
        "requestedBy": user,
        "media": {
            "status": media_status,
            "tmdbId": tmdb,
            "tvdbId": tvdb,
            "mediaAddedAt": media_added,
            "updatedAt": updated,
        },
    }


def iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def poll_with(raw_requests, titles=None, days=7):
    """Run overseerr_events() against a canned request list. `titles` maps
    (tmdbId, tvdbId) -> (title, media_type), as getMediaTitle returns."""
    titles = titles or {}
    digest._poll_cache.update({"at": 0.0, "days": None, "events": []})
    with patch.object(digest.overseerr, "getRequests", return_value=raw_requests), \
         patch.object(digest.overseerr, "getMediaTitle",
                      side_effect=lambda tmdb, tvdb: titles.get((tmdb, tvdb), (None, "movie"))):
        return digest.overseerr_events(days)


def test_poll_surfaces_available_and_pending_without_any_webhook():
    events = poll_with(
        [seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(2), req_id=1),
         seerr_request(tmdb=438631, media_status=3, created=iso_days_ago(3), req_id=2)],
        titles={(603, None): ("The Matrix", "movie"), (438631, None): ("Dune", "movie")},
    )
    by_title = {e["title"]: e for e in events}
    assert by_title["The Matrix"]["event"] == digest.EVENT_AVAILABLE
    assert by_title["Dune"]["event"] == digest.EVENT_ADDED
    assert {e["source"] for e in events} == {"overseerr-poll"}


def test_poll_dates_availability_by_mediaAddedAt_not_createdAt():
    # The regression this whole path turns on: requested months ago, landed
    # yesterday. Windowing on createdAt would hide it from every digest.
    events = poll_with(
        [seerr_request(tmdb=603, media_status=5, created=iso_days_ago(90),
                       media_added=iso_days_ago(1))],
        titles={(603, None): ("The Matrix", "movie")},
    )
    assert [e["title"] for e in events] == ["The Matrix"]


def test_poll_ignores_items_that_landed_before_the_window():
    events = poll_with(
        [seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(30))],
        titles={(603, None): ("The Matrix", "movie")},
    )
    assert events == []


def test_poll_skips_declined_failed_and_unknown():
    events = poll_with(
        [seerr_request(tmdb=1, request_status=3, media_added=iso_days_ago(1)),   # declined
         seerr_request(tmdb=2, request_status=4, media_added=iso_days_ago(1)),   # failed
         seerr_request(tmdb=3, media_status=1, media_added=iso_days_ago(1)),     # unknown
         seerr_request(tmdb=4, media_status=6, media_added=iso_days_ago(1))],    # deleted
        titles={(n, None): (f"Film {n}", "movie") for n in (1, 2, 3, 4)},
    )
    assert events == []


def test_poll_carries_requester_so_personal_dms_still_work():
    routed_scope()
    db.upsertMembership(SCOPE, "1", "alice", status="approved")
    db.approveMembership(SCOPE, "1", approved_by="x")
    db.linkSeerr(SCOPE, "1", 11, seerr_username="alice", seerr_email="a@x.com")

    events = poll_with(
        [seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(1),
                       user={"id": 11, "displayName": "alice", "email": "a@x.com"})],
        titles={(603, None): ("The Matrix", "movie")},
    )
    ctx = type("C", (), {"bot": AsyncMock()})()
    asyncio.run(digest.send_weekly_digest_to_scope(ctx, db.getScope(SCOPE), events))
    dms = [c for c in ctx.bot.send_message.call_args_list if "message_thread_id" not in c.kwargs]
    assert any(c.kwargs["chat_id"] == 1 and "The Matrix" in c.kwargs["text"] for c in dms)


def test_polled_item_dedupes_against_the_webhook_row_and_credits_both():
    db.recordMediaEvent("The Matrix (1999)", "movie", source="overseerr", external_id="tmdb:603")
    polled = poll_with(
        [seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(1))],
        titles={(603, None): ("The Matrix", "movie")},
    )
    b = digest.weekly_breakdown(db.getRecentMediaEvents(7) + polled)
    assert b["counts"]["movie"] == 1
    assert sources_for("The Matrix (1999)", b) == ["overseerr", "overseerr-poll"]


def test_poll_uses_tvdb_for_tv_so_it_dedupes_against_sonarr():
    db.recordMediaEvent("Severance", "tv", source="sonarr", external_id="tvdb:371980")
    polled = poll_with(
        [seerr_request(tmdb=95396, tvdb=371980, media_status=5, media_added=iso_days_ago(1))],
        titles={(95396, 371980): ("Severance", "tv")},
    )
    assert polled[0]["external_id"] == "tvdb:371980"
    b = digest.weekly_breakdown(db.getRecentMediaEvents(7) + polled)
    assert b["counts"]["tv"] == 1


def test_poll_falls_back_to_updatedAt_when_mediaAddedAt_is_absent():
    # Not every Overseerr version sets mediaAddedAt; dropping the item would
    # reproduce the empty digest this path exists to fix.
    events = poll_with(
        [seerr_request(tmdb=603, media_status=5, media_added=None, updated=iso_days_ago(1))],
        titles={(603, None): ("The Matrix", "movie")},
    )
    assert [e["title"] for e in events] == ["The Matrix"]


def test_poll_skips_items_with_no_usable_timestamp():
    events = poll_with(
        [seerr_request(tmdb=603, media_status=5, media_added=None, updated=None)],
        titles={(603, None): ("The Matrix", "movie")},
    )
    assert events == []


def test_poll_skips_untitled_items():
    events = poll_with([seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(1))])
    assert events == []


def test_poll_returns_nothing_when_overseerr_is_disabled():
    cfg.config["overseerr"]["enable"] = False
    try:
        assert poll_with([seerr_request(tmdb=603, media_added=iso_days_ago(1))]) == []
    finally:
        cfg.config["overseerr"]["enable"] = True


def test_poll_survives_an_unreachable_overseerr():
    # getRequests already swallows transport errors and returns []; the point
    # here is that a bad response shape can't take the digest down with it.
    digest._poll_cache.update({"at": 0.0, "days": None, "events": []})
    with patch.object(digest.overseerr, "getRequests", return_value=[{"media": "not-a-dict"}]):
        assert digest.overseerr_events(7) == []


def test_collect_events_merges_recorded_and_polled():
    db.recordMediaEvent("Dune", "movie", source="radarr", external_id="tmdb:438631")
    digest._poll_cache.update({"at": 0.0, "days": None, "events": []})
    with patch.object(digest.overseerr, "getRequests",
                      return_value=[seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(1))]), \
         patch.object(digest.overseerr, "getMediaTitle", return_value=("The Matrix", "movie")):
        events = digest.collect_events(7)
    assert {e["title"] for e in events} == {"Dune", "The Matrix"}


def test_poll_is_cached_so_repeated_miniapp_opens_dont_hammer_overseerr():
    digest._poll_cache.update({"at": 0.0, "days": None, "events": []})
    raw = [seerr_request(tmdb=603, media_status=5, media_added=iso_days_ago(1))]
    with patch.object(digest.overseerr, "getRequests", return_value=raw) as get, \
         patch.object(digest.overseerr, "getMediaTitle", return_value=("The Matrix", "movie")):
        digest.overseerr_events(7)
        digest.overseerr_events(7)
    assert get.call_count == 1
