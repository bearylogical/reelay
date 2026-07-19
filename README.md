# Reelay

[![Tests](https://github.com/bearylogical/reelay/actions/workflows/test.yml/badge.svg)](https://github.com/bearylogical/reelay/actions/workflows/test.yml)
[![Version](https://img.shields.io/github/v/tag/bearylogical/reelay?label=version&sort=semver)](https://github.com/bearylogical/reelay/tags)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

**The household media-request relay for Telegram.** Reelay lets a group request
movies and TV through [Overseerr](https://overseerr.dev/)/Jellyseerr, tracks
those requests, nudges people to actually watch what they asked for, and posts a
weekly "what's new" — all inside one Telegram supergroup, with a Mini App
dashboard on top.

Reelay began as a fork of [Addarr](https://github.com/Waterboy1602/Addarr) and
grew into a group-native, Overseerr-first bot with its own architecture.

---

## What it does

- **Group-native.** Add the bot to a Telegram forum supergroup; it auto-registers
  the group, and a Telegram admin claims it (`/claim`). Members join by DM
  (`/join <code>`), an admin approves, and onboarding links them to their
  Overseerr/Plex account and asks their reminder preference.
- **Per-scope roles.** `member` / `editor` / `admin` per group (replacing flat
  allowlist files). Requests, queue visibility, and admin actions are gated by role.
- **Requests through Overseerr.** The add flow (chat or Mini App) submits to
  Overseerr — attributed to the requesting user — instead of hitting
  Sonarr/Radarr directly, so per-user tracking and watch data work. Falls back to
  direct Sonarr/Radarr when Overseerr isn't configured.
- **Mini App dashboard.** A Telegram Mini App (`/app` or the menu button):
  view your requests, the live download queue (editor/admin), and a **Browse**
  tab to search the catalog and request with one tap. Auth is Telegram's signed
  `initData`, role-filtered server-side.
- **Channel routing.** Pin categories to forum topics: `/routehere requests`
  posts a shared record of each request into your `#requests` topic;
  `/routehere updates` targets the weekly digest.
- **Watched-aware reminders.** N days after a request becomes available, Reelay
  DMs the requester a nudge — *unless* Overseerr's watch data shows they already
  watched it.
- **Weekly "what's new".** The Overseerr webhook records availability events; once
  a week Reelay posts a library-wide roundup into `#updates` and DMs each member
  the items **they** requested that went live.
- **Group-safe inline keyboards, `/switch` for multi-group DMs, and the original
  Sonarr/Radarr list + Transmission/Sabnzbd speed controls.**

---

## Quick start (Docker)

```bash
git clone <this-repo> reelay && cd reelay
cp config_example.yaml config.yaml   # fill in telegram.token at minimum
touch reelay.db                      # so the volume mounts as a file
docker compose up -d reelay
```

Minimum config is a Telegram bot token (`telegram.token`) and your Sonarr/Radarr
details. Overseerr, the Mini App, reminders, and the weekly digest are each
opt-in blocks in `config.yaml`.

### Enabling the Mini App / webhook (public HTTPS)

Telegram requires HTTPS for Mini Apps, and Overseerr needs a reachable webhook
URL. The shipped `docker-compose.yml` includes a `cloudflared` sidecar:

1. Create a Cloudflare tunnel, route a hostname to `http://localhost:8080`.
2. Put its token in `CLOUDFLARE_TUNNEL_TOKEN` (a `.env` file works).
3. Set `miniapp.url: https://<host>/miniapp/` and `miniapp.enable: true`.
4. For the digest: set `overseerr.webhookSecret`, point Overseerr's Webhook agent
   at `https://<host>/overseerr/webhook/<secret>`, and `/routehere updates` in
   `#updates`.

---

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp config_example.yaml config.yaml   # fill in a token
python -m reelay
```

---

## Architecture

Single process, single SQLite file (`reelay.db`), no ORM. The Telegram bot runs
on long polling; a small **aiohttp** server (started on the same loop) serves the
Mini App and the Overseerr webhook. Background jobs (reminders, weekly digest)
run on python-telegram-bot's `JobQueue`.

| Module | Responsibility |
|--------|----------------|
| `bot.py` | Entry point, handler/job registration, group + scope commands |
| `conversation.py` | Shared conversation helpers (`stop`, `getService`, states) — breaks the add/delete import cycle |
| `db.py` | SQLite schema + queries (scopes, memberships, seerr links, routes, reminder & media events) |
| `commons.py` | Auth, inline-keyboard owner-locking, scope resolution, API helpers |
| `overseerr.py` | Overseerr/Jellyseerr client (search, request, users, watch data, counts) |
| `radarr.py` / `sonarr.py` | Direct Sonarr/Radarr client (lookup, add, delete, queue) |
| `onboarding.py` | `/join`, approvals, account linking, `/remindme` |
| `channels.py` | Category → forum-topic routing |
| `reminders`* | Watched-aware nudge job (in `bot.py`) |
| `digest.py` | Weekly what's-new (group post + personal DMs) |
| `webhooks.py` | Overseerr webhook receiver (records availability) |
| `miniapp.py` | aiohttp server, initData auth, dashboard/catalog/request API |
| `add.py`* / `delete.py` / `listing.py` / `transmission.py` / `sabnzbd.py` | Conversation flows |

\* the add flow currently lives in `bot.py`.

---

## Credits

Derived from **Addarr** by Wannes Van de Putte (MIT). Request-tracking and Mini
App patterns were informed by the Overseerr-Telegram-Bot and baca projects.
Licensed under the [MIT License](LICENSE).
