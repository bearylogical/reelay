import pytest

import reelay.config as cfg
import reelay.db as db


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
    db.initDb()
    yield
