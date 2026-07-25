import pytest

import reelay.config as cfg
import reelay.db as db
import reelay.digest as digest
import reelay.overseerr as overseerr


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Point the DB at a throwaway file and set predictable config for each test."""
    cfg.config["telegram"] = {"token": "testtoken"}
    cfg.config["overseerr"] = {"enable": True, "url": "http://fake", "apikey": "k", "webhookSecret": "s3cr3t"}
    cfg.config["miniapp"] = {"enable": True, "url": "https://x/miniapp/", "listenHost": "127.0.0.1", "listenPort": 0}
    cfg.config["weeklyDigest"] = {"enable": True, "day": "monday", "hour": 9}
    # Mutated in place, not replaced: radarr.py/sonarr.py bind their sub-dict at
    # import time, so rebinding cfg.config["radarr"] wouldn't reach them.
    cfg.config.setdefault("radarr", {})["webhookSecret"] = "r4d4rr"
    cfg.config.setdefault("sonarr", {})["webhookSecret"] = "s0n4rr"
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    # digest.collect_events() polls Overseerr, and overseerr.url points at a
    # host that doesn't exist -- left alone, every digest test would spend its
    # time failing a DNS lookup. Tests that care about the poll patch this with
    # their own requests; everything else gets "Overseerr had nothing".
    monkeypatch.setattr(overseerr, "getRequests", lambda *a, **kw: [])
    # Module-level TTL cache: without this, one test's poll result leaks into
    # the next (the DB is fresh per test, so this would be a confusing lie).
    digest._poll_cache.update({"at": 0.0, "days": None, "events": []})
    db.initDb()
    yield
