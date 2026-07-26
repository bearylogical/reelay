"""Run the Mini App UI in an ordinary browser, with no Telegram involved.

    python -m reelay.miniapp_dev            # seeded demo data, fake services
    python -m reelay.miniapp_dev --role member
    python -m reelay.miniapp_dev --live     # talk to the real Overseerr/Radarr/Sonarr in config.yaml

The Mini App is normally unreachable outside Telegram for two reasons, and this
module removes both:

1. **No signed initData.** Telegram signs an `initData` blob with the bot token
   and the client sends it on every request; a browser has none, so every
   endpoint 401s and the page renders "Couldn't load. missing initData". This
   sets REELAY_MINIAPP_DEV, which makes miniapp._authed() fall back to acting
   as a real member from the database (see miniapp.dev_mode()).
2. **Nothing to be a member of.** Auth resolves a *scope* and a *membership*;
   a fresh checkout has neither. `--seed` writes a throwaway scope, a member
   per role, and a week of media events into a separate dev database
   (reelay-dev.db), so the real reelay.db is never touched.

By default the outbound services are faked, so the UI renders realistic content
with no Overseerr/Radarr/Sonarr running and nothing is ever really requested.
`--live` turns that off and uses whatever config.yaml points at -- useful for
checking a real search or queue, but note that requesting then genuinely
submits to Overseerr.

Telegram sends (join approvals, request announcements) have no bot here: they
are printed to the console instead, which is usually what you wanted to see.
"""

import argparse
import logging
import os
import sys

from aiohttp import web

# Set before importing miniapp so its module-level logger and every _authed()
# call see dev mode from the very first request.
os.environ.setdefault("REELAY_MINIAPP_DEV", "1")

from . import db  # noqa: E402
from . import definitions  # noqa: E402

DEV_DB_PATH = os.path.join(definitions.ROOT_DIR, "reelay-dev.db")

DEV_SCOPE_CHAT_ID = "-1009999000001"
DEV_SCOPE_TITLE = "Dev Household"
# One user per role, so `--role` switches what the UI is allowed to show
# without re-seeding. The ids are deliberately obvious in logs.
DEV_USERS = {
    "admin": ("900001", "dev_admin"),
    "editor": ("900002", "dev_editor"),
    "member": ("900003", "dev_member"),
}


def seed():
    """Write a self-contained scope, three members and a week of library
    events. Idempotent -- upserts, so re-running keeps whatever you toggled
    in the UI."""
    db.initDb()
    db.upsertScope(DEV_SCOPE_CHAT_ID, title=DEV_SCOPE_TITLE)
    for role, (user_id, username) in DEV_USERS.items():
        db.upsertMembership(DEV_SCOPE_CHAT_ID, user_id, username, role=role, status="approved")
        db.approveMembership(DEV_SCOPE_CHAT_ID, user_id, approved_by="dev-seed", role=role)
        db.setReminderThreshold(DEV_SCOPE_CHAT_ID, user_id, 3)
        db.setAnonymizeRequests(DEV_SCOPE_CHAT_ID, user_id, False)
    # A pending member and a pending chat-access request, so the Members tab
    # has its approve/deny rows to look at rather than an empty list.
    db.upsertMembership(DEV_SCOPE_CHAT_ID, "900004", "dev_pending", status="pending")
    db.requestChatAccess("-1009999000002", display_name="Some Other Group")
    # Link the admin so the Browse tab can be exercised end to end (an
    # unlinked user gets "not_linked" back from /api/request).
    db.linkSeerr(DEV_SCOPE_CHAT_ID, DEV_USERS["admin"][0], 1, seerr_username="dev-plex-user")
    # Routes point back at the dev scope itself, so "Send now" and a Browse
    # request print their group post to the console instead of no-oping.
    db.setChannelRoute(DEV_SCOPE_CHAT_ID, "requests", DEV_SCOPE_CHAT_ID)
    db.setChannelRoute(DEV_SCOPE_CHAT_ID, "updates", DEV_SCOPE_CHAT_ID)

    if not db.getRecentMediaEvents(7):
        for title, media_type, source, event in [
            ("Dune: Part Two", "movie", "overseerr", "available"),
            ("Dune: Part Two", "movie", "radarr", "available"),
            ("The Bear", "tv", "sonarr", "available"),
            ("Shogun", "tv", "overseerr", "available"),
            ("Poor Things", "movie", "reelay", "added"),
        ]:
            db.recordMediaEvent(title, media_type, source=source, event=event)


# --- Fake outbound services ----------------------------------------------------
#
# Everything below stands in for a service the dev box doesn't run. Patched
# onto the real modules (not injected) because the endpoints call them by
# module attribute, and this keeps the production code path identical.

_FAKE_REQUESTS = [
    {"id": 1, "title": "Dune: Part Two", "mediaType": "movie", "mediaStatus": 5,
     "statusLabel": "✅ Available", "requestedById": 1, "createdAt": "2026-07-20T10:00:00Z"},
    {"id": 2, "title": "The Bear", "mediaType": "tv", "mediaStatus": 4,
     "statusLabel": "🟡 Partially available", "requestedById": 1, "createdAt": "2026-07-21T10:00:00Z"},
    {"id": 3, "title": "Poor Things", "mediaType": "movie", "mediaStatus": 3,
     "statusLabel": "⏳ Processing", "requestedById": 2, "createdAt": "2026-07-22T10:00:00Z"},
]

_FAKE_CATALOG = [
    {"id": 693134, "title": "Dune: Part Two", "year": "2024", "poster": None, "status": 5},
    {"id": 438631, "title": "Dune", "year": "2021", "poster": None, "status": 5},
    {"id": 1160419, "title": "Dune: Part Three", "year": "2026", "poster": None, "status": None},
]

_FAKE_QUEUE = [
    {"title": "Poor Things (2023)", "mediaType": "movie", "progress": 62,
     "timeleft": "00:12:30", "status": "downloading"},
    {"title": "Shogun S01E07", "mediaType": "tv", "progress": 8,
     "timeleft": "01:40:00", "status": "downloading"},
]

_FAKE_DIAGNOSTICS = {
    "ok": False,
    "checks": [
        {"service": "Radarr", "status": "ok", "summary": "Reachable, 2 root folders, 3 quality profiles", "detail": ""},
        {"service": "Sonarr", "status": "ok", "summary": "Reachable, 1 root folder, 3 quality profiles", "detail": ""},
        {"service": "Overseerr", "status": "ok", "summary": "Reachable, API key accepted", "detail": ""},
        {"service": "Overseerr → Radarr", "status": "warn",
         "summary": "Points at a different Radarr than Reelay does",
         "detail": "No root folder path in common (fake data)"},
        {"service": "Overseerr webhook", "status": "ok", "summary": "Last delivered 2 hours ago", "detail": ""},
        {"service": "Sonarr webhook", "status": "skip", "summary": "No webhookSecret configured", "detail": ""},
    ],
}


def install_fakes():
    from . import diagnostics
    from . import digest
    from . import overseerr
    from . import radarr
    from . import sonarr

    # The "this week" card then reflects exactly the seeded media_events --
    # the poll would otherwise try to reshape _FAKE_REQUESTS, which aren't
    # raw Overseerr objects, and log a warning per row.
    digest.overseerr_events = lambda days=7: []

    overseerr.enabled = lambda: True
    overseerr.getRequestCount = lambda: {"pending": 1, "processing": 1, "available": 2, "total": 4}
    overseerr.getRequests = lambda *a, **kw: list(_FAKE_REQUESTS)
    overseerr.summarizeRequests = lambda raw, cache=None: list(raw)
    overseerr.search = lambda q, media_type: [
        dict(m, title=m["title"]) for m in _FAKE_CATALOG
        if q.lower() in m["title"].lower() or not q
    ]
    overseerr.getPosterUrl = lambda media_type, media_id: None
    overseerr.createRequest = lambda *a, **kw: {"id": 99}
    overseerr.getUsers = lambda: [{"id": 1, "displayName": "dev-plex-user"}]
    radarr.getQueue = lambda: [q for q in _FAKE_QUEUE if q["mediaType"] == "movie"]
    sonarr.getQueue = lambda: [q for q in _FAKE_QUEUE if q["mediaType"] == "tv"]
    diagnostics.run = lambda: dict(_FAKE_DIAGNOSTICS)


class ConsoleBot:
    """Stands in for the python-telegram-bot Bot the endpoints hold. Every
    outbound message is printed instead of sent -- an approve/deny or a Mini
    App request shows you exactly what the member would have received."""

    def __init__(self):
        self.username = "reelay_dev_bot"

    async def send_message(self, chat_id, text, **kwargs):
        print(f"\n  [telegram → {chat_id}] {text}\n", flush=True)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        print(f"\n  [telegram photo → {chat_id}] {caption or ''} ({photo})\n", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m reelay.miniapp_dev",
        description="Serve the Reelay Mini App UI in a normal browser, no Telegram required.",
    )
    parser.add_argument("--port", type=int, default=8081, help="port to listen on (default 8081)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1 -- this server has no auth)")
    parser.add_argument("--role", choices=sorted(DEV_USERS), default="admin",
                        help="which role to browse as (default admin, the only one that sees the Members tab)")
    parser.add_argument("--db", default=DEV_DB_PATH,
                        help=f"database file to use (default {DEV_DB_PATH}; the real reelay.db is left alone)")
    parser.add_argument("--no-seed", action="store_true", help="don't write demo data into the dev database")
    parser.add_argument("--live", action="store_true",
                        help="use the real Overseerr/Radarr/Sonarr from config.yaml instead of fakes "
                             "(requests you submit are real)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    db.DB_PATH = args.db
    definitions.DB_PATH = args.db
    if not args.no_seed:
        seed()
    else:
        db.initDb()

    user_id, _ = DEV_USERS[args.role]
    os.environ["REELAY_MINIAPP_DEV"] = "1"
    os.environ["REELAY_MINIAPP_DEV_USER"] = user_id

    if not args.live:
        install_fakes()

    from . import miniapp
    if not miniapp.dev_mode():  # pragma: no cover - defensive
        sys.exit("REELAY_MINIAPP_DEV did not take effect; refusing to start an unusable dev server.")

    app = miniapp.build_app(ConsoleBot())
    print(
        f"\nReelay Mini App dev server\n"
        f"  url      http://{args.host}:{args.port}/miniapp/\n"
        f"  acting as {DEV_USERS[args.role][1]} ({user_id}), role={args.role}\n"
        f"  database {args.db}\n"
        f"  services {'REAL (config.yaml)' if args.live else 'faked'}\n"
        f"\nAuth is bypassed here. Bound to {args.host}; do not expose this.\n",
        flush=True,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
