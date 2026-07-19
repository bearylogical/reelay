import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .definitions import CHATID_PATH, DB_PATH

_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # unambiguous, no 0/O/1/I/L


def _now():
    return datetime.now(timezone.utc).isoformat()


def generateInviteCode(length=8):
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(length))


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initDb():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scopes (
                chat_id             TEXT PRIMARY KEY,
                message_thread_id   TEXT,
                title               TEXT,
                invite_code         TEXT UNIQUE NOT NULL,
                join_policy         TEXT NOT NULL DEFAULT 'approval'
                                        CHECK (join_policy IN ('auto', 'approval')),
                is_active           INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memberships (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_chat_id               TEXT NOT NULL REFERENCES scopes(chat_id) ON DELETE CASCADE,
                user_id                     TEXT NOT NULL,
                username                    TEXT,
                role                        TEXT NOT NULL DEFAULT 'member'
                                                CHECK (role IN ('member', 'editor', 'admin')),
                status                      TEXT NOT NULL DEFAULT 'pending'
                                                CHECK (status IN ('pending', 'approved', 'denied', 'banned')),
                reminder_threshold_days     INTEGER,
                requested_at                TEXT NOT NULL,
                approved_at                 TEXT,
                approved_by                 TEXT,
                UNIQUE(scope_chat_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);

            CREATE TABLE IF NOT EXISTS seerr_links (
                scope_chat_id   TEXT NOT NULL REFERENCES scopes(chat_id) ON DELETE CASCADE,
                user_id         TEXT NOT NULL,
                seerr_user_id   INTEGER NOT NULL,
                seerr_username  TEXT,
                mode            TEXT NOT NULL DEFAULT 'api'
                                    CHECK (mode IN ('api', 'normal', 'shared')),
                session_cookie  TEXT,
                linked_at       TEXT NOT NULL,
                PRIMARY KEY (scope_chat_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_seerr_links_seerr_user ON seerr_links(scope_chat_id, seerr_user_id);

            CREATE TABLE IF NOT EXISTS reminder_state (
                seerr_request_id   INTEGER NOT NULL,
                seerr_media_id     INTEGER NOT NULL,
                scope_chat_id      TEXT NOT NULL REFERENCES scopes(chat_id) ON DELETE CASCADE,
                user_id            TEXT NOT NULL,
                title              TEXT,
                media_type         TEXT CHECK (media_type IN ('movie', 'tv')),
                available_since    TEXT NOT NULL,
                reminder_sent_at   TEXT,
                resolved           TEXT NOT NULL DEFAULT 'pending'
                                        CHECK (resolved IN ('pending', 'watched', 'reminded', 'unknown')),
                PRIMARY KEY (scope_chat_id, seerr_request_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_state_pending ON reminder_state(resolved) WHERE resolved = 'pending';

            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id                 TEXT PRIMARY KEY,
                active_scope_chat_id    TEXT REFERENCES scopes(chat_id)
            );

            -- Where the bot posts a given category of output within a scope.
            -- category is e.g. 'requests' (extensible to 'reminders',
            -- 'approvals'). dest_chat_id + dest_thread_id target a Telegram
            -- forum topic (thread) or a separate chat.
            CREATE TABLE IF NOT EXISTS channel_routes (
                scope_chat_id   TEXT NOT NULL REFERENCES scopes(chat_id) ON DELETE CASCADE,
                category        TEXT NOT NULL,
                dest_chat_id    TEXT NOT NULL,
                dest_thread_id  TEXT,
                PRIMARY KEY (scope_chat_id, category)
            );

            -- Availability events recorded from the Overseerr webhook, digested
            -- into the weekly "what's new" (group) + personal DMs. requester
            -- fields let us attribute an item to the member who requested it.
            CREATE TABLE IF NOT EXISTS media_events (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                title                   TEXT,
                media_type              TEXT,
                requested_by_username   TEXT,
                requested_by_email      TEXT,
                occurred_at             TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_media_events_time ON media_events(occurred_at);

            -- Bot-wide (not per-scope) authorization for the legacy direct
            -- Sonarr/Radarr/Transmission/Sabnzbd commands. Replaces the old
            -- shared-password chatid.txt file with an admin-approved request,
            -- reviewed from the Mini App.
            CREATE TABLE IF NOT EXISTS chat_access_requests (
                chat_id         TEXT PRIMARY KEY,
                display_name    TEXT,
                status          TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'approved', 'denied')),
                requested_at    TEXT NOT NULL,
                decided_at      TEXT,
                decided_by      TEXT
            );
            """
        )
        # seerr_email added after the fact -- reliable key for matching a
        # weekly event's requester back to a Telegram member. Guarded so it's
        # idempotent on databases created by an earlier build.
        try:
            conn.execute("ALTER TABLE seerr_links ADD COLUMN seerr_email TEXT")
        except sqlite3.OperationalError:
            pass

    _migrateLegacyChatIds()


def _migrateLegacyChatIds():
    """One-time (idempotent) import of the old chatid.txt allowlist into
    chat_access_requests, so upgrading doesn't strand existing deployments
    without access. Safe to run on every startup -- INSERT OR IGNORE."""
    if not os.path.exists(CHATID_PATH):
        return
    with open(CHATID_PATH, "r") as file:
        chatIds = [line.strip("\n").split(" - ")[0] for line in file if line.strip("\n")]
    if not chatIds:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO chat_access_requests"
            " (chat_id, display_name, status, requested_at, decided_at, decided_by)"
            " VALUES (?, NULL, 'approved', ?, ?, 'legacy-migration')",
            [(chatId, _now(), _now()) for chatId in chatIds],
        )


# --- scopes -----------------------------------------------------------------

def upsertScope(chat_id, title=None, message_thread_id=None, join_policy="approval"):
    with _connect() as conn:
        row = conn.execute("SELECT chat_id FROM scopes WHERE chat_id = ?", (str(chat_id),)).fetchone()
        if row:
            conn.execute(
                "UPDATE scopes SET title = COALESCE(?, title), is_active = 1 WHERE chat_id = ?",
                (title, str(chat_id)),
            )
        else:
            code = generateInviteCode()
            while conn.execute("SELECT 1 FROM scopes WHERE invite_code = ?", (code,)).fetchone():
                code = generateInviteCode()
            conn.execute(
                "INSERT INTO scopes (chat_id, message_thread_id, title, invite_code, join_policy, is_active, created_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?)",
                (str(chat_id), message_thread_id, title, code, join_policy, _now()),
            )
    # Read after the `with` block exits (and commits) -- getScope() opens its
    # own connection, which otherwise wouldn't see the uncommitted write yet.
    return getScope(chat_id)


def getScope(chat_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM scopes WHERE chat_id = ?", (str(chat_id),)).fetchone()
        return dict(row) if row else None


def getScopeByInviteCode(code):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM scopes WHERE invite_code = ?", (code,)).fetchone()
        return dict(row) if row else None


def setScopeActive(chat_id, is_active):
    with _connect() as conn:
        conn.execute(
            "UPDATE scopes SET is_active = ? WHERE chat_id = ?",
            (1 if is_active else 0, str(chat_id)),
        )


def setJoinPolicy(chat_id, join_policy):
    with _connect() as conn:
        conn.execute("UPDATE scopes SET join_policy = ? WHERE chat_id = ?", (join_policy, str(chat_id)))
    return getScope(chat_id)


def rotateInviteCode(chat_id):
    with _connect() as conn:
        code = generateInviteCode()
        while conn.execute("SELECT 1 FROM scopes WHERE invite_code = ?", (code,)).fetchone():
            code = generateInviteCode()
        conn.execute("UPDATE scopes SET invite_code = ? WHERE chat_id = ?", (code, str(chat_id)))
    return getScope(chat_id)


def getActiveScopes():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM scopes WHERE is_active = 1").fetchall()
        return [dict(r) for r in rows]


# --- memberships --------------------------------------------------------------

def upsertMembership(scope_chat_id, user_id, username=None, role="member", status="pending"):
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM memberships WHERE scope_chat_id = ? AND user_id = ?",
            (str(scope_chat_id), str(user_id)),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE memberships SET username = COALESCE(?, username) WHERE id = ?",
                (username, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO memberships (scope_chat_id, user_id, username, role, status, requested_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (str(scope_chat_id), str(user_id), username, role, status, _now()),
            )
    return getMembership(scope_chat_id, user_id)


def getMembership(scope_chat_id, user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM memberships WHERE scope_chat_id = ? AND user_id = ?",
            (str(scope_chat_id), str(user_id)),
        ).fetchone()
        return dict(row) if row else None


def approveMembership(scope_chat_id, user_id, approved_by, role=None):
    with _connect() as conn:
        if role:
            conn.execute(
                "UPDATE memberships SET status = 'approved', approved_at = ?, approved_by = ?, role = ?"
                " WHERE scope_chat_id = ? AND user_id = ?",
                (_now(), str(approved_by), role, str(scope_chat_id), str(user_id)),
            )
        else:
            conn.execute(
                "UPDATE memberships SET status = 'approved', approved_at = ?, approved_by = ?"
                " WHERE scope_chat_id = ? AND user_id = ?",
                (_now(), str(approved_by), str(scope_chat_id), str(user_id)),
            )
    return getMembership(scope_chat_id, user_id)


def getApprovedMemberships(user_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT m.*, s.title AS scope_title FROM memberships m"
            " JOIN scopes s ON s.chat_id = m.scope_chat_id"
            " WHERE m.user_id = ? AND m.status = 'approved' AND s.is_active = 1"
            " ORDER BY m.approved_at DESC",
            (str(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def setReminderThreshold(scope_chat_id, user_id, days):
    with _connect() as conn:
        conn.execute(
            "UPDATE memberships SET reminder_threshold_days = ? WHERE scope_chat_id = ? AND user_id = ?",
            (days, str(scope_chat_id), str(user_id)),
        )


def getMembershipsAwaitingReminderAnswer(user_id):
    """Approved memberships for this user that haven't answered the
    onboarding reminder-threshold question yet (persisted signal, so it
    survives a bot restart -- no in-memory per-user flag needed)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND status = 'approved'"
            " AND reminder_threshold_days IS NULL",
            (str(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def getMemberships(scope_chat_id):
    """All memberships (any status), pending first, for the admin roster view."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memberships WHERE scope_chat_id = ?"
            " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, requested_at",
            (str(scope_chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def setMembershipRole(scope_chat_id, user_id, role):
    with _connect() as conn:
        conn.execute(
            "UPDATE memberships SET role = ? WHERE scope_chat_id = ? AND user_id = ?",
            (role, str(scope_chat_id), str(user_id)),
        )
    return getMembership(scope_chat_id, user_id)


def removeMembership(scope_chat_id, user_id):
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM memberships WHERE scope_chat_id = ? AND user_id = ?",
            (str(scope_chat_id), str(user_id)),
        )
        return cur.rowcount > 0


def getApprovedAdmins(scope_chat_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memberships WHERE scope_chat_id = ? AND role = 'admin' AND status = 'approved'",
            (str(scope_chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def getApprovedMembers(scope_chat_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memberships WHERE scope_chat_id = ? AND status = 'approved'",
            (str(scope_chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def denyMembership(scope_chat_id, user_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE memberships SET status = 'denied' WHERE scope_chat_id = ? AND user_id = ?",
            (str(scope_chat_id), str(user_id)),
        )


# --- seerr_links --------------------------------------------------------------

def linkSeerr(scope_chat_id, user_id, seerr_user_id, seerr_username=None, seerr_email=None, mode="api", session_cookie=None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO seerr_links (scope_chat_id, user_id, seerr_user_id, seerr_username, seerr_email, mode, session_cookie, linked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(scope_chat_id, user_id) DO UPDATE SET"
            " seerr_user_id = excluded.seerr_user_id, seerr_username = excluded.seerr_username,"
            " seerr_email = excluded.seerr_email, mode = excluded.mode, session_cookie = excluded.session_cookie",
            (str(scope_chat_id), str(user_id), seerr_user_id, seerr_username, seerr_email, mode, session_cookie, _now()),
        )


def getSeerrLink(scope_chat_id, user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM seerr_links WHERE scope_chat_id = ? AND user_id = ?",
            (str(scope_chat_id), str(user_id)),
        ).fetchone()
        return dict(row) if row else None


def getSeerrLinkByOverseerrUser(scope_chat_id, seerr_user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM seerr_links WHERE scope_chat_id = ? AND seerr_user_id = ?",
            (str(scope_chat_id), seerr_user_id),
        ).fetchone()
        return dict(row) if row else None


def getApprovedMembersWithoutSeerrLink(scope_chat_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT m.* FROM memberships m"
            " LEFT JOIN seerr_links sl ON sl.scope_chat_id = m.scope_chat_id AND sl.user_id = m.user_id"
            " WHERE m.scope_chat_id = ? AND m.status = 'approved' AND sl.user_id IS NULL",
            (str(scope_chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


# --- reminder_state -----------------------------------------------------------

def getReminderState(scope_chat_id, seerr_request_id, user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM reminder_state WHERE scope_chat_id = ? AND seerr_request_id = ? AND user_id = ?",
            (str(scope_chat_id), seerr_request_id, str(user_id)),
        ).fetchone()
        return dict(row) if row else None


def createReminderStatePending(scope_chat_id, seerr_request_id, seerr_media_id, user_id, title=None, media_type=None):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminder_state"
            " (seerr_request_id, seerr_media_id, scope_chat_id, user_id, title, media_type, available_since, resolved)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (seerr_request_id, seerr_media_id, str(scope_chat_id), str(user_id), title, media_type, _now()),
        )


def markReminderResolved(scope_chat_id, seerr_request_id, user_id, resolved, sent=False):
    with _connect() as conn:
        if sent:
            conn.execute(
                "UPDATE reminder_state SET resolved = ?, reminder_sent_at = ?"
                " WHERE scope_chat_id = ? AND seerr_request_id = ? AND user_id = ?",
                (resolved, _now(), str(scope_chat_id), seerr_request_id, str(user_id)),
            )
        else:
            conn.execute(
                "UPDATE reminder_state SET resolved = ?"
                " WHERE scope_chat_id = ? AND seerr_request_id = ? AND user_id = ?",
                (resolved, str(scope_chat_id), seerr_request_id, str(user_id)),
            )


def getPendingReminderStates(scope_chat_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminder_state WHERE scope_chat_id = ? AND resolved = 'pending'",
            (str(scope_chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


# --- user_prefs ----------------------------------------------------------------

def setActiveScope(user_id, scope_chat_id):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, active_scope_chat_id) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET active_scope_chat_id = excluded.active_scope_chat_id",
            (str(user_id), str(scope_chat_id)),
        )


def getActiveScope(user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT active_scope_chat_id FROM user_prefs WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        return row["active_scope_chat_id"] if row else None


# --- channel_routes -----------------------------------------------------------

def setChannelRoute(scope_chat_id, category, dest_chat_id, dest_thread_id=None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO channel_routes (scope_chat_id, category, dest_chat_id, dest_thread_id)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(scope_chat_id, category) DO UPDATE SET"
            " dest_chat_id = excluded.dest_chat_id, dest_thread_id = excluded.dest_thread_id",
            (str(scope_chat_id), category, str(dest_chat_id),
             str(dest_thread_id) if dest_thread_id is not None else None),
        )


def getChannelRoute(scope_chat_id, category):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM channel_routes WHERE scope_chat_id = ? AND category = ?",
            (str(scope_chat_id), category),
        ).fetchone()
        return dict(row) if row else None


def getChannelRoutes(scope_chat_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_routes WHERE scope_chat_id = ?", (str(scope_chat_id),)
        ).fetchall()
        return [dict(r) for r in rows]


def deleteChannelRoute(scope_chat_id, category):
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM channel_routes WHERE scope_chat_id = ? AND category = ?",
            (str(scope_chat_id), category),
        )
        return cur.rowcount > 0


# --- media_events (weekly digest source) --------------------------------------

def recordMediaEvent(title, media_type, requested_by_username=None, requested_by_email=None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO media_events (title, media_type, requested_by_username, requested_by_email, occurred_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, media_type, requested_by_username, requested_by_email, _now()),
        )


def getRecentMediaEvents(days=7):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM media_events WHERE occurred_at >= datetime('now', ?) ORDER BY occurred_at",
            (f"-{int(days)} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def pruneMediaEvents(days=30):
    with _connect() as conn:
        conn.execute("DELETE FROM media_events WHERE occurred_at < datetime('now', ?)", (f"-{int(days)} days",))


# --- chat_access_requests -------------------------------------------------------
#
# Bot-wide authorization for the legacy direct Sonarr/Radarr/Transmission/
# Sabnzbd commands (replaces the old shared-password chatid.txt file). Not
# scoped to a particular group -- reviewed by an admin of any active scope
# from the Mini App.

def requestChatAccess(chat_id, display_name=None):
    """Ensure a pending request exists for chat_id. Returns True if this call
    put it into (or freshly created) a pending state -- i.e. the caller should
    notify admins. Returns False if it was already pending (repeat attempt,
    don't re-notify) or already approved."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM chat_access_requests WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO chat_access_requests (chat_id, display_name, status, requested_at)"
                " VALUES (?, ?, 'pending', ?)",
                (str(chat_id), display_name, _now()),
            )
            return True
        if row["status"] == "denied":
            conn.execute(
                "UPDATE chat_access_requests SET display_name = ?, status = 'pending',"
                " requested_at = ?, decided_at = NULL, decided_by = NULL WHERE chat_id = ?",
                (display_name, _now(), str(chat_id)),
            )
            return True
        return False


def isChatAuthorized(chat_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM chat_access_requests WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return bool(row and row["status"] == "approved")


def getPendingChatAccessRequests():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_access_requests WHERE status = 'pending' ORDER BY requested_at"
        ).fetchall()
        return [dict(r) for r in rows]


def approveChatAccess(chat_id, approved_by):
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_access_requests SET status = 'approved', decided_at = ?, decided_by = ?"
            " WHERE chat_id = ?",
            (_now(), str(approved_by), str(chat_id)),
        )


def denyChatAccess(chat_id, denied_by):
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_access_requests SET status = 'denied', decided_at = ?, decided_by = ?"
            " WHERE chat_id = ?",
            (_now(), str(denied_by), str(chat_id)),
        )


def getApprovedChatIds():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM chat_access_requests WHERE status = 'approved'"
        ).fetchall()
        return [r["chat_id"] for r in rows]
