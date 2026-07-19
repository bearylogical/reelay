from unittest.mock import MagicMock, patch

import reelay.overseerr as ov


def _resp(payload, status=200):
    return MagicMock(status_code=status, raise_for_status=lambda: None, json=lambda: payload)


def test_search_filters_and_normalizes():
    payload = {"results": [
        {"id": 100, "mediaType": "movie", "title": "The Matrix", "releaseDate": "1999-03-31",
         "posterPath": "/m.jpg", "mediaInfo": {"status": 5}},
        {"id": 101, "mediaType": "tv", "name": "Show", "firstAirDate": "2015-01-01", "mediaInfo": {}},
        {"id": 102, "mediaType": "person"},
    ]}
    with patch("reelay.overseerr.requests.get", return_value=_resp(payload)):
        movies = ov.search("x", "movie")
        tv = ov.search("x", "tv")
    assert [m["id"] for m in movies] == [100]
    assert movies[0]["year"] == "1999" and movies[0]["poster"].endswith("/m.jpg")
    assert [t["id"] for t in tv] == [101]


def test_create_request_payload():
    captured = {}

    def post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _resp({"id": 1})

    with patch("reelay.overseerr.requests.post", side_effect=post):
        ov.createRequest("tv", 55, requested_by_seerr_id=3, seasons="all")
    assert captured["json"] == {"mediaType": "tv", "mediaId": 55, "is4k": False, "userId": 3, "seasons": "all"}


def test_watch_data_union_and_empty():
    ok = {"data": {"users": [{"id": 1}]}, "data4k": {"users": [{"id": 2}]}}
    with patch("reelay.overseerr.requests.get", return_value=_resp(ok)):
        assert ov.getWatchedUserIds(5) == {1, 2}
    empty = {"data": {"users": []}, "data4k": {"users": []}}
    with patch("reelay.overseerr.requests.get", return_value=_resp(empty)):
        assert ov.getWatchedUserIds(5) is None


def test_status_badge():
    assert ov.statusBadge(5).endswith("Available")
    assert "Not requested" in ov.statusBadge(None)


def test_summarize_requests_resolves_titles():
    raw = [{"id": 1, "media": {"id": 9, "tmdbId": 603, "tvdbId": None, "status": 5},
            "requestedBy": {"id": 7}, "status": 2, "createdAt": "2026-01-01"}]
    with patch("reelay.overseerr.getMediaTitle", return_value=("The Matrix", "movie")):
        out = ov.summarizeRequests(raw)
    assert out[0]["title"] == "The Matrix" and out[0]["requestedById"] == 7


def test_sign_in_with_plex_success():
    resp = _resp({"id": 5, "displayName": "bob", "email": "b@x.com"})
    resp.cookies = {"connect.sid": "abc123"}
    with patch("reelay.overseerr.requests.post", return_value=resp):
        user, cookie = ov.signInWithPlex("plex-token")
    assert user["id"] == 5 and cookie == "abc123"


def test_sign_in_with_plex_failure():
    with patch("reelay.overseerr.requests.post", side_effect=Exception("boom")):
        user, cookie = ov.signInWithPlex("plex-token")
    assert user is None and cookie is None
