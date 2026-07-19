# Reelay

[![Tests](https://github.com/bearylogical/reelay/actions/workflows/test.yml/badge.svg)](https://github.com/bearylogical/reelay/actions/workflows/test.yml)
[![Version](https://img.shields.io/github/v/tag/bearylogical/reelay?label=version&sort=semver)](https://github.com/bearylogical/reelay/tags)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

**The household media-request relay for Telegram.** Reelay lets a group request
movies and TV through [Overseerr](https://overseerr.dev/)/Jellyseerr, tracks
those requests, nudges people to actually watch what they asked for, and posts a
weekly "what's new" — all inside one Telegram group, with a Mini App
dashboard on top.

Reelay began as a fork of [Addarr](https://github.com/Waterboy1602/Addarr) and
grew into a group-native, Overseerr-first bot with its own architecture.

---

## What it does

- **Group-native, one scope per group.** Add the bot to a Telegram group or
  supergroup: it auto-registers a "scope" for that chat. If whoever added it
  is already a Telegram admin there, they're activated as the scope's admin
  immediately; otherwise any real Telegram admin runs `/claim` in the group to
  activate it and become the Reelay admin. Either way the bot replies with an
  **invite code** for that scope.
- **Members join by DM.** A member runs `/join <code>` in a DM with the bot.
  Whether they're auto-approved or need sign-off depends on the scope's join
  policy (`approval` by default, or `auto` — an admin toggles this in the Mini
  App's **Members** tab): under `approval`, admins get a DM with Approve/Deny
  buttons (also actionable from the Members tab). Once approved they're asked
  a reminder-threshold question (0–30 days; 0 disables reminders) and offered
  a picker to link their Overseerr/Plex account (skippable). Members can
  (re-)link anytime with `/linkme` or self-service Plex OAuth on the Mini
  App's **Account** tab; admins can nudge everyone who hasn't linked yet with
  `/requestlink`.
- **Per-scope roles.** `member` / `editor` / `admin`, managed from the Mini
  App's **Members** tab (approve/deny joins, change roles, remove members,
  regenerate the invite code) — replacing flat allowlist files. `editor`/
  `admin` see the live download queue; `admin` also manages members, roles,
  channel routing, and the invite code. The last remaining admin of a scope
  can't be demoted or removed.
- **In-group requests are opt-in.** By default `/start` inside a group just
  points members at a DM, so requests always go through a linked account — an
  admin flips "Requests on in group" for that scope in the Members tab to
  allow requesting directly in the chat.
- **Requests through Overseerr.** The add flow (DM, group when enabled, or
  Mini App) submits to Overseerr — attributed to the requesting user — instead
  of hitting Sonarr/Radarr directly, so per-user tracking and watch data work.
  Falls back to direct Sonarr/Radarr when Overseerr isn't configured.
- **Mini App dashboard.** A Telegram Mini App (`/app` or the menu button) with
  five tabs: **Requests** (yours, plus live counts), **Queue** (editor/admin —
  live Sonarr/Radarr download queue), **Members** (admin-only — see above),
  **Browse** (search the catalog and request with one tap), and **Account**
  (self-service Plex linking). Auth is Telegram's signed `initData`,
  role-filtered server-side — no separate login.
- **Legacy chat-access requests.** A chat that isn't part of any scope yet and
  hits a gated command (or runs `/auth`) is queued as a pending chat-access
  request — no shared password anymore. Any scope admin reviews it from the
  Mini App's Members tab (Approve/Deny); previously approved chats show up
  under "Open chats" there and can be revoked.
- **Channel routing.** Inside a forum topic, an admin runs `/routehere
  requests` to post a shared record of each request there, or `/routehere
  updates` to target the weekly digest; `/routes` lists current routing,
  `/unroute <category>` clears one.
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
| `bot.py` | Entry point, handler/job registration, group scope activation (`/claim`, auto-register), `/switch`, `/app` |
| `conversation.py` | Shared conversation helpers (`stop`, `getService`, states) — breaks the add/delete import cycle |
| `db.py` | SQLite schema + queries: scopes, memberships/roles, invite codes, join policy, per-scope feature flags (`FEATURE_*`, e.g. `allowGroupRequests`), seerr links, chat-access requests, channel routes, reminder & media events |
| `commons.py` | Auth, inline-keyboard owner-locking, scope resolution, legacy `requestChatAccess` (pending chat-access requests, no password), API helpers |
| `overseerr.py` | Overseerr/Jellyseerr client (search, request, users, watch data, counts, Plex sign-in) |
| `radarr.py` / `sonarr.py` | Direct Sonarr/Radarr client (lookup, add, delete, queue) |
| `plex.py` | Plex.tv PIN-based OAuth ("Sign in with Plex") for the Mini App's self-service account linking |
| `onboarding.py` | `/join`, join approvals, Overseerr/Plex account linking (`/linkme`, `/requestlink`), reminder-threshold Q&A |
| `channels.py` | Category (`requests`/`updates`) → forum-topic routing: `/routehere`, `/routes`, `/unroute` |
| `reminders`* | Watched-aware nudge job (in `bot.py`) |
| `digest.py` | Weekly what's-new (group post + personal DMs) |
| `webhooks.py` | Overseerr webhook receiver (records availability) |
| `miniapp.py` | aiohttp server, initData auth; dashboard/queue/catalog/request API; admin API for members, roles, invite code, join policy, feature flags, and chat-access requests; Plex linking API |
| `add.py`* / `delete.py` / `listing.py` / `transmission.py` / `sabnzbd.py` | Conversation flows |

\* the add flow currently lives in `bot.py`.

---

## Credits

Derived from **Addarr** by Wannes Van de Putte (MIT). Request-tracking and Mini
App patterns were informed by the Overseerr-Telegram-Bot and baca projects.
Licensed under the [MIT License](LICENSE).
