import reelay.db as db


def test_scope_upsert_and_membership():
    s = db.upsertScope("-100111", title="Fam")
    assert s["invite_code"]
    s2 = db.upsertScope("-100111", title="Renamed")
    assert s2["invite_code"] == s["invite_code"]  # stable code
    assert s2["title"] == "Renamed"

    db.upsertMembership("-100111", "5", "alice", role="admin", status="pending")
    db.approveMembership("-100111", "5", approved_by="5", role="admin")
    m = db.getMembership("-100111", "5")
    assert m["status"] == "approved" and m["role"] == "admin"

    # re-upsert updates username without clobbering role/status
    db.upsertMembership("-100111", "5", "alice2")
    m2 = db.getMembership("-100111", "5")
    assert m2["username"] == "alice2" and m2["role"] == "admin" and m2["status"] == "approved"


def test_seerr_link_roundtrip():
    db.upsertScope("-100111", title="Fam")
    db.linkSeerr("-100111", "5", 42, seerr_username="bob", seerr_email="b@x.com")
    link = db.getSeerrLink("-100111", "5")
    assert link["seerr_user_id"] == 42 and link["seerr_email"] == "b@x.com"
    assert db.getSeerrLinkByOverseerrUser("-100111", 42)["user_id"] == "5"


def test_channel_routes():
    db.upsertScope("-100111", title="Fam")
    db.setChannelRoute("-100111", "requests", "-100111", "7")
    assert db.getChannelRoute("-100111", "requests")["dest_thread_id"] == "7"
    assert db.deleteChannelRoute("-100111", "requests") is True
    assert db.getChannelRoute("-100111", "requests") is None


def test_media_events():
    db.recordMediaEvent("The Matrix", "movie", "bob", "b@x.com")
    events = db.getRecentMediaEvents(7)
    assert len(events) == 1 and events[0]["title"] == "The Matrix"


def test_reminder_state_lifecycle():
    db.upsertScope("-100111", title="Fam")
    db.createReminderStatePending("-100111", 9, 900, "5", title="X", media_type="movie")
    assert db.getReminderState("-100111", 9, "5")["resolved"] == "pending"
    db.markReminderResolved("-100111", 9, "5", "reminded", sent=True)
    st = db.getReminderState("-100111", 9, "5")
    assert st["resolved"] == "reminded" and st["reminder_sent_at"]


def test_reminder_threshold_awaiting():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "5", "a", status="approved")
    db.approveMembership("-100111", "5", approved_by="x")
    assert len(db.getMembershipsAwaitingReminderAnswer("5")) == 1
    db.setReminderThreshold("-100111", "5", 3)
    assert db.getMembershipsAwaitingReminderAnswer("5") == []


def test_pending_memberships():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "6", "carol", status="pending")
    db.upsertMembership("-100111", "7", "dave", status="approved")
    db.approveMembership("-100111", "7", approved_by="x")
    pending = db.getPendingMemberships("-100111")
    assert len(pending) == 1 and pending[0]["user_id"] == "6"


def test_set_membership_role():
    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "5", "a", status="approved")
    db.approveMembership("-100111", "5", approved_by="x")
    db.setMembershipRole("-100111", "5", "editor")
    assert db.getMembership("-100111", "5")["role"] == "editor"
