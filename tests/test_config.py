"""Startup config checking, and who hears about a failure.

A missing key blocks startup and messages people, so the cost of getting this
wrong is high in both directions: too strict and every existing install is
told it's broken by a feature it doesn't use; too loud and every user gets an
operator's problem in their DMs.
"""

import copy as copymod

import reelay.commons as commons
import reelay.config as cfg
import reelay.db as db


def _legacy_config(drop=()):
    """A deep copy of the example config with `drop` (slash-separated, any
    depth) removed -- standing in for a config.yaml written before those keys
    existed. Deep, so a nested pop can't mutate config_example itself."""
    config = copymod.deepcopy(cfg.config_example)
    for path in drop:
        *parents, key = path.split("/")
        target = config
        for parent in parents:
            target = target[parent]
        del target[key]
    return config


def test_optional_keys_absent_do_not_report_missing(monkeypatch):
    # The reported bug: adding radarr/sonarr webhookSecret to the example made
    # every pre-existing config.yaml fail startup and DM everyone about it.
    monkeypatch.setattr(cfg, "config", _legacy_config(
        ("radarr/webhookSecret", "sonarr/webhookSecret", "overseerr/webhookSecret")))
    assert cfg.checkConfig() == []


def test_required_key_absent_is_still_reported(monkeypatch):
    monkeypatch.setattr(cfg, "config", _legacy_config(("telegram/token",)))
    assert cfg.checkConfig() == ["telegram/token"]


def test_optional_and_required_missing_reports_only_the_required(monkeypatch):
    monkeypatch.setattr(cfg, "config", _legacy_config(
        ("radarr/webhookSecret", "sonarr/auth/apikey")))
    assert cfg.checkConfig() == ["sonarr/auth/apikey"]


def test_every_optional_key_actually_exists_in_the_example():
    # Guards against a typo in OPTIONAL_KEYS silently un-guarding a real key.
    example = cfg.flatten_dict(cfg.config_example)
    assert cfg.OPTIONAL_KEYS <= set(example)


def test_admin_ids_come_from_config_admin_file_and_scope_admins(tmp_path, monkeypatch):
    monkeypatch.setitem(cfg.config, "adminNotifyId", 111)

    admin_file = tmp_path / "admin.txt"
    admin_file.write_text("222 - alice\nbob_by_name - bob\n")  # usernames aren't addressable
    monkeypatch.setattr(commons, "ADMIN_PATH", str(admin_file))

    db.upsertScope("-100111", title="Fam")
    db.upsertMembership("-100111", "333", "carol", role="admin", status="approved")
    db.approveMembership("-100111", "333", approved_by="x", role="admin")
    db.upsertMembership("-100111", "444", "dave", status="approved")  # plain member
    db.approveMembership("-100111", "444", approved_by="x")

    admins = commons.getAdminChatIds()
    assert admins == ["111", "222", "333"]
    assert "444" not in admins  # a member must not be told about config problems


def test_admin_ids_empty_when_nothing_identifies_an_admin(tmp_path, monkeypatch):
    # Caller logs instead of falling back to broadcasting at every user.
    monkeypatch.setitem(cfg.config, "adminNotifyId", None)
    monkeypatch.setattr(commons, "ADMIN_PATH", str(tmp_path / "nope.txt"))
    assert commons.getAdminChatIds() == []


def test_admin_ids_survive_an_uninitialised_database(tmp_path, monkeypatch):
    # startCheck() runs before initDb(); a fresh install must still reach the
    # operator via config rather than blowing up.
    monkeypatch.setitem(cfg.config, "adminNotifyId", 111)
    monkeypatch.setattr(commons, "ADMIN_PATH", str(tmp_path / "nope.txt"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "empty.db"))
    assert commons.getAdminChatIds() == ["111"]
