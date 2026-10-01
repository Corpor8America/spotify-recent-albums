"""Integration tests for the expired-album web workflow."""

from datetime import datetime, timedelta
from unittest.mock import patch

import spotify_core as core
from spotify_core.models import Album, State


def _album(album_id, release_date, *, added=False, override=None, uris=None):
    return Album(
        id=album_id,
        name="Test Album",
        artist="Test Artist",
        artist_id="artist1",
        album_type="album",
        release_date=release_date,
        url="",
        total_tracks=2,
        first_seen="",
        manual_override=override,
        added_to_playlist=added,
        track_uris=list(uris or []),
    )


def _date_days_ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def test_expired_album_dashboard_and_promotion(client, ctx, monkeypatch):
    """An expired album is shown, promoted, and remains tracked."""
    core.save_state(State(known_albums={
        "expired1": _album("expired1", _date_days_ago(400)),
        "current1": _album("current1", _date_days_ago(20)),
        "retired1": _album("retired1", _date_days_ago(800)),
    }))
    core.save_config({**core.load_config(), "days_lookback": 365})

    monkeypatch.setattr(core, "is_configured", lambda: True)
    monkeypatch.setattr(core, "is_connected", lambda: True)
    monkeypatch.setattr(core, "get_recent_logs", lambda: [])
    monkeypatch.setattr(core, "apply_album_override", lambda album_id, value: (
        setattr(core.load_state().known_albums[album_id], "manual_override", False) or True
    ))

    response = client.get("/")
    assert response.status_code == 200
    assert b"Test Album" in response.data
    assert b"Expired" in response.data

    response = client.post("/albums/expired1/promote")
    assert response.status_code == 302


def test_return_promoted_album_to_expired(client, monkeypatch):
    """Returning a promoted album removes it from the playlist and resets its override."""
    album = _album(
        "expired1", _date_days_ago(400), added=True, override=False,
        uris=["spotify:track:one", "spotify:track:two"],
    )
    core.save_state(State(known_albums={"expired1": album}))
    core.save_config({
        **core.load_config(),
        "days_lookback": 365,
        "spotify_playlist_id": "playlist123",
        "spotify_client_id": "client",
        "spotify_client_secret": "secret",
    })
    monkeypatch.setattr(core, "get_access_token", lambda *args: "token")
    monkeypatch.setattr(core, "load_refresh_token", lambda: "refresh")
    removed = []
    monkeypatch.setattr(
        core, "remove_tracks_from_playlist",
        lambda token, playlist_id, uris, state: removed.append((playlist_id, list(uris))),
    )

    response = client.post("/albums/expired1/expire")

    assert response.status_code == 302
    assert removed == [("playlist123", ["spotify:track:one", "spotify:track:two"])]
    saved = core.load_state().known_albums["expired1"]
    assert saved.added_to_playlist is False
    assert saved.track_uris == []
    assert saved.manual_override is None


def test_expired_actions_reject_album_outside_expired_window(client, monkeypatch):
    """An album older than the retention window cannot be promoted or returned."""
    core.save_state(State(known_albums={
        "too_old": _album("too_old", _date_days_ago(800)),
    }))
    core.save_config({**core.load_config(), "days_lookback": 365})
    monkeypatch.setattr(core, "is_connected", lambda: True)

    assert client.post("/albums/too_old/promote").status_code == 404
    assert client.post("/albums/too_old/expire").status_code == 404
