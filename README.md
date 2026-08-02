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
  regenerate the invite code, run the connection self-test) — replacing flat
  allowlist files. `editor`/
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
- **Connection self-test.** The Members tab's **Connections** card runs a
  read-only check of the whole chain — Reelay → Radarr/Sonarr, Reelay →
  Overseerr, Overseerr → Radarr/Sonarr (a live probe through Overseerr's own
  API), plus whether each inbound webhook is configured and still delivering —
  and reports one line per link instead of leaving you to read three services'
  logs. See [Checking the connections](#checking-the-connections).
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
- **Weekly "what's new".** Reelay records library events from every source you
  wire up — the Overseerr webhook, the Radarr/Sonarr webhooks, and its own
  `/start` add flow — and dedupes across them, so something added straight in
  Radarr still shows up. Once a week it posts a library-wide roundup into
  `#updates` (split into what's watchable now and what's coming soon) and DMs
  each member the items **they** requested that went live.
- **Commands that introduce themselves.** Reelay publishes Telegram's `/`
  command menu on every start — one list for DMs, another for groups — so the
  commands are discoverable without reading this file. (It sets them via the
  API, so it replaces whatever you typed into BotFather's `/setcommands`.)
  `/help` itself is assembled per caller: a member isn't shown admin commands,
  a group isn't shown DM-only ones, and a download client that's switched off
  isn't advertised.
- **`/cat`.** A random cat picture from [cataas.com](https://cataas.com) or a
  random fact from [catfact.ninja](https://catfact.ninja), one or the other at
  random, in a DM or a group. No key and nothing to configure; if one service
  is down you get the other kind of cat. (It has more to say when `theme: cat`
  is on.)
- **Group-safe inline keyboards, `/switch` for multi-group DMs, and the original
  Sonarr/Radarr list + Transmission/Sabnzbd speed controls.**

---

## Quick start (Docker)

```bash
git clone <this-repo> reelay && cd reelay
make config-init                     # copies config_example.yaml -> config.yaml
touch reelay.db                      # so the volume mounts as a file
docker compose up -d reelay
```

Minimum config is a Telegram bot token (`telegram.token`) and your Sonarr/Radarr
details. Overseerr, the Mini App, reminders, and the weekly digest are each
opt-in blocks in `config.yaml`.

### Personality (optional)

`theme` in `config.yaml` picks how the bot words things. `default` is the plain
wording. `cat` gives it a cat's voice — it meows when a film lands, drags in the
weekly roundup, and naps when a command finishes:

```yaml
theme: cat   # default or cat
```

It's cosmetic only: no command, button, or behaviour changes, every instruction
still names the same commands, and error and config messages stay plain. Restart
to apply.

The lines a regular sees often — request confirmations, the weekly roundup, group
announcements — have several wordings and pick one at random each time, never the
one that key used last, so the bot doesn't recite the same sentence every day:

> ✅ Requested Dune — I'll meow when it's ready to watch. Track it any time in /app.
> ✅ On the hunt for Brazil. I'll meow when it's ready to watch; /app tracks it in the meantime.

Themes are overlays on the English copy in `translations/themes/<theme>/`, listing
only the strings they reword — anything they leave out keeps the neutral version.
A YAML list instead of a single string is what makes a key varied, and any
translation file can use one. Adding a theme is a new directory plus `THEMES` in
`reelay/definitions.py`; with a non-English `language` set, only the strings that
fall back to English are themed.

Tests hold the copy to its job: every variant of a string has to keep the
placeholders (`%{title}`) and command names (`/linkme`) of the plain wording it
replaces, so no rewording can turn an instruction into just a joke.

### Keeping config.yaml current

`config_example.yaml` is the schema of record, and new releases add keys to it.
Reelay refuses to start if `config.yaml` is missing a required one, so check for
that **before** you restart rather than after:

```bash
make config-check     # what your config.yaml is missing, and what would block startup
make config-diff      # the exact lines a migration would add
make config-migrate   # add them (backs config.yaml up first)
```

The migration edits the file as text: your values, ordering and comments are
left exactly as they are, and each new key arrives with the example's comment
explaining what it does. Keys you have that the example doesn't are reported and
never touched. Optional keys (the `webhookSecret`s) are called out separately —
missing ones don't block startup, they just leave that feature off.

### Enabling the Mini App / webhook (public HTTPS)

Telegram requires HTTPS for Mini Apps, and the webhook senders need a reachable
URL. The shipped `docker-compose.yml` includes a `cloudflared` sidecar:

1. Create a Cloudflare tunnel, route a hostname to `http://localhost:8080`.
2. Put its token in `CLOUDFLARE_TUNNEL_TOKEN` (a `.env` file works).
3. Set `miniapp.url: https://<host>/miniapp/` and `miniapp.enable: true`.
4. Run `/routehere updates` in your `#updates` topic — **without this the digest
   has nowhere to post** (Reelay logs a warning when that happens).
5. Wire up whichever sources you use. Each secret is any long random string, and
   each service's "Test" button confirms the round trip in `#updates`:

   | Source | Config key | URL | Where |
   |---|---|---|---|
   | Overseerr | `overseerr.webhookSecret` | `https://<host>/overseerr/webhook/<secret>` | Settings → Notifications → Webhook |
   | Radarr | `radarr.webhookSecret` | `https://<host>/radarr/webhook/<secret>` | Settings → Connect → Webhook, tick *On Import* (and optionally *On Movie Added*) |
   | Sonarr | `sonarr.webhookSecret` | `https://<host>/sonarr/webhook/<secret>` | Settings → Connect → Webhook, tick *On Import* (and optionally *On Series Add*) |

   Imports count as **watchable now**; the *Added* events only mean it's wanted,
   so they're listed under "coming soon". Quality upgrades are skipped. Adds made
   through Reelay's own `/start` flow are recorded with no webhook needed.

   To check a source is actually delivering, expand the Mini App's **This week**
   card: each title is tagged with the service(s) that reported it, and the
   footer tallies them (`Reported by Overseerr 4 · Radarr 2`). The group post
   deliberately omits this — it's a diagnostic, not something `#updates` needs.

### Checking the connections

Members tab → **Connections** → **Run test**. Every check is a `GET`; nothing is
created or changed, so it's safe to run whenever something looks wrong.

| Row | What it proves |
|---|---|
| Radarr / Sonarr | Reelay can reach it, the API key is accepted, and it has root folders and quality profiles (both needed before an add can succeed) |
| Overseerr | Reelay can reach it *and* `overseerr.apikey` is accepted — `/status` alone is public, so the test makes an authenticated call too |
| Overseerr → Radarr / Sonarr | Overseerr can talk to its own Radarr/Sonarr **right now** — the check asks Overseerr to fetch that server's profiles and root folders, so a reply means the link works |
| Radarr / Sonarr / Overseerr webhook | Whether a `webhookSecret` is set, and when that source last actually delivered an event (nothing outbound can test an inbound webhook) |

Statuses are ✅ working, ⚠️ worth a look, ❌ broken, ⚪ not configured. Only ❌
counts against the overall verdict — an unconfigured service is a choice, not a
fault.

One ⚠️ is worth explaining: *"Points at a different Radarr than Reelay does."*
Hostnames can't settle this (Overseerr in Docker calls it `radarr:7878` for the
box Reelay reaches at `192.168.1.5:7878`), so the test compares root folder
paths instead — a property of the instance, not of the network. No overlap means
the queue Reelay shows and the library Overseerr fills are probably two
different servers.

---

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
make install                         # pip install -e ".[test]"
make config-init                     # fill in a token
make run                             # config-check, then python -m reelay
```

`make` on its own lists every target.

### Working on the Mini App without Telegram

The Mini App normally can't be opened in a browser: Telegram signs an
`initData` blob with the bot token and the page sends it on every request, so
without it every endpoint 401s and you get *"Couldn't load. missing initData"*.
Waiting on a tunnel and a phone to change one CSS rule is no way to work, so
there's a dev server:

```bash
make miniapp-dev
```

That serves the real UI and the real API at <http://127.0.0.1:8081/miniapp/>,
with **no bot token, no tunnel and no Telegram**. It:

- seeds a throwaway scope, one member per role, a pending join, a chat-access
  request and a week of library events into **`reelay-dev.db`** — your real
  `reelay.db` is never touched;
- fakes Overseerr/Radarr/Sonarr, so requests, the queue, search and the
  connection self-test all render believable data with none of them running;
- prints anything the bot would have sent to Telegram (join approvals, request
  announcements, the weekly digest) to the console instead;
- marks the page with a **dev mode · auth bypassed** badge, so a dev tab can't
  be mistaken for the real thing.

Useful flags (`make miniapp-dev ARGS="…"`, or run the module directly):

| Flag | What it does |
|---|---|
| `--role member\|editor\|admin` | Browse as that role. Role filtering is *not* bypassed — as a `member` the Queue and Members tabs stay hidden and their endpoints still 403, so you see what that role really sees |
| `--live` | Use the real Overseerr/Radarr/Sonarr from `config.yaml` instead of fakes. Requests you submit are then real |
| `--port` / `--host` | Defaults `8081` / `127.0.0.1` |
| `--no-seed` | Don't write demo data |

Under the hood this is one environment variable, `REELAY_MINIAPP_DEV=1`, which
makes the Mini App server accept a request carrying *no* initData by acting as
a member from the database (`REELAY_MINIAPP_DEV_USER`, else the first approved
admin). You can set it on the real bot too, to click through the UI against
your live data. Two things to know before you do:

- Signed initData is still verified. Dev mode only fills the gap where a
  browser sent none, so a forged blob can't ride in on it.
- It is an env var, never a `config.yaml` key, precisely because `config.yaml`
  is the file that gets copied to servers. With it set, the whole Mini App API
  is unauthenticated — the bot logs a warning on every start, and you should
  not expose that port.

---

## Architecture

Single process, single SQLite file (`reelay.db`), no ORM. The Telegram bot runs
on long polling; a small **aiohttp** server (started on the same loop) serves the
Mini App and the Overseerr/Radarr/Sonarr webhooks. Background jobs (reminders, weekly digest)
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
| `webhooks.py` | Overseerr/Radarr/Sonarr webhook receivers (record library events for the digest) |
| `diagnostics.py` | Read-only connection self-test of the Overseerr → Radarr/Sonarr chain (Members tab → Connections) |
| `cat.py` | `/cat` — a random cat picture (cataas.com) or cat fact (catfact.ninja) |
| `miniapp.py` | aiohttp server, initData auth; dashboard/queue/catalog/request API; admin API for members, roles, invite code, join policy, feature flags, chat-access requests, and the connection self-test; Plex linking API |
| `miniapp_dev.py` | Dev-only: serves the Mini App in a plain browser with seeded data and faked services ([above](#working-on-the-mini-app-without-telegram)) |
| `add.py`* / `delete.py` / `listing.py` / `transmission.py` / `sabnzbd.py` | Conversation flows |

\* the add flow currently lives in `bot.py`.

---

## Credits

Derived from **Addarr** by Wannes Van de Putte (MIT). Request-tracking and Mini
App patterns were informed by the Overseerr-Telegram-Bot and baca projects.
Licensed under the [MIT License](LICENSE).
