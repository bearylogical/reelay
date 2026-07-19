import pytest

import reelay.config as cfg
import reelay.db as db


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Point the DB at a throwaway file and set predictable config for each test."""
    cfg.config["telegram"] = {"token": "testtoken", "password": "pw"}
    cfg.config["overseerr"] = {"enable": True, "url": "http://fake", "apikey": "k", "webhookSecret": "s3cr3t"}
    cfg.config["miniapp"] = {"enable": True, "url": "https://x/miniapp/", "listenHost": "127.0.0.1", "listenPort": 0}
    cfg.config["weeklyDigest"] = {"enable": True, "day": "monday", "hour": 9}
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.initDb()
    yield
