from unittest.mock import MagicMock, patch

import pytest

import reelay.config as cfg
import reelay.db as db
import reelay.diagnostics as diag


@pytest.fixture(autouse=True)
def arr_config():
    """A fully-configured Radarr/Sonarr, so each test only has to break the one
    thing it's about. conftest already sets up Overseerr and the DB."""
    for app, port in (("radarr", 7878), ("sonarr", 8989)):
        cfg.config.setdefault(app, {}).update({
            "server": {"addr": "10.0.0.5", "port": port, "path": "/", "ssl": False},
            "auth": {"apikey": "key"},
        })
    yield


def _resp(payload=None, status=200, text=""):
    return MagicMock(status_code=status, text=text, json=lambda: payload)


def _byId(result):
    return {c["id"]: c for c in result["checks"]}


def _overseerrOk(path, timeout=None):
    """Everything Overseerr is asked for, all healthy, sharing /movies and /tv
    with the *arr root folders the modules are patched to return below."""
    if path == "/status":
        return {"version": "1.33.0"}, None
    if path == "/request/count":
        return {"total": 0}, None
    if path.startswith("/settings/"):
        return [{"id": 0, "name": "Main", "isDefault": True, "is4k": False}], None
    if path.startswith("/service/radarr"):
        return {"profiles": [{"id": 1}], "rootFolders": [{"path": "/movies"}]}, None
    if path.startswith("/service/sonarr"):
        return {"profiles": [{"id": 1}], "rootFolders": [{"path": "/tv"}]}, None
    raise AssertionError(f"unexpected Overseerr path {path}")


def _run(overseerr_get=_overseerrOk, arr_get=None, radarr_roots=None, sonarr_roots=None):
    arr_get = arr_get or (lambda url, timeout=None: _resp({"version": "5.0"}))
    radarr_roots = radarr_roots if radarr_roots is not None else [{"path": "/movies"}]
    sonarr_roots = sonarr_roots if sonarr_roots is not None else [{"path": "/tv"}]
    with patch("reelay.diagnostics.requests.get", side_effect=arr_get), \
         patch("reelay.overseerr.apiGet", side_effect=overseerr_get), \
         patch("reelay.radarr.getRootFolders", return_value=radarr_roots), \
         patch("reelay.sonarr.getRootFolders", return_value=sonarr_roots), \
         patch("reelay.radarr.getQualityProfiles", return_value=[{"id": 1}]), \
         patch("reelay.sonarr.getQualityProfiles", return_value=[{"id": 1}]):
        return diag.run()


def test_all_healthy():
    res = _run()
    checks = _byId(res)
    assert res["ok"] is True
    assert checks["radarr"]["status"] == "ok" and "5.0" in checks["radarr"]["summary"]
    assert checks["sonarr"]["status"] == "ok"
    assert checks["overseerr"]["status"] == "ok"
    assert checks["overseerr-radarr"]["status"] == "ok"
    assert checks["overseerr-sonarr"]["status"] == "ok"


def test_arr_unreachable_is_a_failure():
    def boom(url, timeout=None):
        raise OSError("connection refused")

    res = _run(arr_get=boom)
    checks = _byId(res)
    assert res["ok"] is False
    assert checks["radarr"]["status"] == "fail" and "connection refused" in checks["radarr"]["detail"]


def test_arr_bad_api_key_is_distinguished_from_unreachable():
    res = _run(arr_get=lambda url, timeout=None: _resp(status=401))
    assert _byId(res)["radarr"]["summary"] == "API key rejected"


def test_arr_not_configured_is_skipped_not_failed():
    cfg.config["sonarr"]["server"]["addr"] = ""
    res = _run()
    checks = _byId(res)
    assert checks["sonarr"]["status"] == "skip"
    # A *arr nobody configured must not drag the overall verdict down.
    assert res["ok"] is True


def test_arr_without_root_folders_warns():
    res = _run(radarr_roots=[])
    checks = _byId(res)
    assert checks["radarr"]["status"] == "warn" and "root folders" in checks["radarr"]["summary"]
    assert res["ok"] is True  # reachable, just not usable yet


def test_overseerr_reachable_but_key_rejected():
    def get(path, timeout=None):
        if path == "/status":
            return {"version": "1.33.0"}, None
        return None, "HTTP 403 — Overseerr rejected the API key"

    res = _run(overseerr_get=get)
    checks = _byId(res)
    assert res["ok"] is False
    assert checks["overseerr"]["status"] == "fail" and "API key" in checks["overseerr"]["summary"]


def test_overseerr_cannot_reach_its_radarr():
    def get(path, timeout=None):
        if path.startswith("/service/radarr"):
            return None, "HTTP 500"
        return _overseerrOk(path)

    res = _run(overseerr_get=get)
    checks = _byId(res)
    assert res["ok"] is False
    assert checks["overseerr-radarr"]["status"] == "fail"
    # The Radarr link is down, not Radarr itself -- don't muddy the other rows.
    assert checks["radarr"]["status"] == "ok"
    assert checks["overseerr-sonarr"]["status"] == "ok"


def test_overseerr_with_no_arr_configured():
    def get(path, timeout=None):
        if path == "/settings/sonarr":
            return [], None
        return _overseerrOk(path)

    res = _run(overseerr_get=get)
    assert _byId(res)["overseerr-sonarr"]["status"] == "fail"


def test_mismatched_root_folders_warn_about_a_different_instance():
    res = _run(radarr_roots=[{"path": "/mnt/other-movies"}])
    checks = _byId(res)
    assert checks["overseerr-radarr"]["status"] == "warn"
    assert "different Radarr" in checks["overseerr-radarr"]["summary"]
    assert res["ok"] is True  # suspicious, not proven broken


def test_shared_root_folder_is_enough_despite_different_hostnames():
    """Overseerr in Docker calls it "radarr:7878"; Reelay reaches the same box
    at 10.0.0.5. Only the root folders can tell us it's one instance."""
    def get(path, timeout=None):
        if path.startswith("/service/radarr"):
            return {"profiles": [{"id": 1}], "rootFolders": [{"path": "/movies"}, {"path": "/4k"}]}, None
        return _overseerrOk(path)

    res = _run(overseerr_get=get)
    assert _byId(res)["overseerr-radarr"]["status"] == "ok"


def test_overseerr_disabled_skips_its_rows():
    cfg.config["overseerr"]["enable"] = False
    res = _run(overseerr_get=lambda p, timeout=None: (None, "Overseerr isn't enabled in config.yaml"))
    checks = _byId(res)
    assert checks["overseerr"]["status"] == "skip"
    assert checks["overseerr-radarr"]["status"] == "skip"
    assert res["ok"] is True


def test_webhook_rows_report_configured_and_delivering():
    db.recordMediaEvent("Dune", "movie", source="radarr", event="available")
    res = _run()
    checks = _byId(res)
    assert checks["webhook-radarr"]["status"] == "ok"
    # Secret set (conftest) but nothing has ever arrived from Sonarr.
    assert checks["webhook-sonarr"]["status"] == "warn"
    assert res["ok"] is True  # a quiet webhook never fails the whole test


def test_webhook_without_a_secret_is_skipped():
    cfg.config["sonarr"]["webhookSecret"] = ""
    res = _run()
    assert _byId(res)["webhook-sonarr"]["status"] == "skip"
