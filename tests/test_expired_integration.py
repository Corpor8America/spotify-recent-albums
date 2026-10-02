"""Integration tests for the expired-album web workflow.

Covers the dashboard rendering plus the promote / return-to-expired actions,
including the failure paths that used to surface as 500s or clobbered a
running scan.
"""

from datetime import datetime, timedelta

import pytest

import spotify_core as core
from spotify_core import auth as auth_mod
from spotify_core import playlists as playlists_mod
from spotify_core.models import Album, State

DAYS_LOOKBACK = 365
# Release-date buckets, relative to now.
ACTIVE = 10
EXPIRED = 400
RETIRED = 900


def _album(album_id, name, days_ago, *, added=False, override=None, uris=None, auto=False):
    return Album(
        id=album_id,
        name=name,
        artist="Test Artist",
        artist_id="artist1",
        album_type="album",
        release_date=(datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
        url=f"https://open.spotify.com/album/{album_id}",
        total_tracks=2,
        first_seen="",
        auto_excluded=auto,
        manual_override=override,
        added_to_playlist=added,
        track_uris=list(uris or []),
    )


@pytest.fixture
def connected_client(client):
    """A client configured for the expired workflow and connected to Spotify.
    Spotify itself is never contacted; tests patch the network calls."""
    core.save_config({
        **core.load_config(),
        "days_lookback": DAYS_LOOKBACK,
        "spotify_playlist_id": "playlist123",
    })
    core.save_refresh_token("refresh-token")
    return client


@pytest.fixture
def offline_spotify(monkeypatch):
    """Patch every outbound Spotify call used by these actions.

    ``core.*`` and ``auth_mod``/``playlists_mod`` are both patched: the routes
    go through the bound ``core`` wrappers, while ``apply_album_override``
    reaches for the submodule functions directly.
    """
    added, removed = [], []
    monkeypatch.setattr(core, "get_access_token", lambda *args: "token")
    monkeypatch.setattr(core, "load_refresh_token", lambda: "refresh-token")
    monkeypatch.setattr(core, "get_album_track_uris",
                        lambda album_id, state: ["t:1", "t:2"])
    monkeypatch.setattr(auth_mod, "get_access_token", lambda *args: "token")
    monkeypatch.setattr(auth_mod, "load_refresh_token", lambda *args: "refresh-token")
    monkeypatch.setattr(playlists_mod, "get_album_track_uris",
                        lambda ctx, token, album_id, state: ["t:1", "t:2"])
    monkeypatch.setattr(playlists_mod, "get_playlist_track_uris", lambda *args: [])
    monkeypatch.setattr(playlists_mod, "add_tracks_to_playlist",
                        lambda ctx, token, pid, uris, state: added.append((pid, list(uris))))
    monkeypatch.setattr(core, "add_tracks_to_playlist",
                        lambda token, pid, uris, state: added.append((pid, list(uris))))
    monkeypatch.setattr(core, "remove_tracks_from_playlist",
                        lambda token, pid, uris, state: removed.append((pid, list(uris))))
    return added, removed


def _dashboard(client, monkeypatch):
    monkeypatch.setattr(core, "is_configured", lambda: True)
    monkeypatch.setattr(core, "get_recent_logs", list)
    response = client.get("/")
    assert response.status_code == 200
    return response.data.decode()


# --- Dashboard ----------------------------------------------------------------


def test_dashboard_lists_only_albums_inside_the_expired_window(connected_client, monkeypatch):
    core.save_state(State(known_albums={
        "active": _album("active", "Fresh Release", ACTIVE),
        "expired": _album("expired", "Aged Release", EXPIRED),
        "retired": _album("retired", "Ancient Release", RETIRED),
    }))

    html = _dashboard(connected_client, monkeypatch)

    assert "Aged Release" in html
    assert "Fresh Release" in html          # still in the recent-releases table
    assert "Ancient Release" not in html    # past retention: not listed anywhere
    assert "Expired albums (1)" in html


def test_expired_album_is_not_also_listed_as_excluded(connected_client, monkeypatch):
    core.save_state(State(known_albums={
        "old_live": _album("old_live", "Old Concert (Live)", EXPIRED, auto=True),
        "new_live": _album("new_live", "New Concert (Live)", ACTIVE, auto=True),
    }))

    html = _dashboard(connected_client, monkeypatch)

    assert html.count("Old Concert (Live)") == 1
    assert html.count("New Concert (Live)") == 1
    assert "Expired albums (1)" in html
    assert "Excluded albums (1)" in html


def test_promoted_album_stays_in_the_expired_list(connected_client, monkeypatch):
    core.save_state(State(known_albums={
        "p": _album("p", "Promoted Release", EXPIRED, added=True, override=False,
                    uris=["t:1"]),
    }))

    html = _dashboard(connected_client, monkeypatch)

    assert "Promoted Release" in html
    assert "Expired albums (1)" in html
    assert "Remove from playlist" in html


# --- Promote ------------------------------------------------------------------


def test_promote_adds_the_album_back_to_the_playlist(connected_client, offline_spotify):
    added, _ = offline_spotify
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED),
    }))

    response = connected_client.post("/albums/e/promote")

    assert response.status_code == 302
    assert added == [("playlist123", ["t:1", "t:2"])]
    saved = core.load_state().known_albums["e"]
    assert saved.manual_override is False
    assert saved.added_to_playlist is True


@pytest.mark.parametrize("album_id", ["active", "retired", "missing"])
def test_promote_rejects_albums_outside_the_expired_window(connected_client, offline_spotify,
                                                          album_id):
    core.save_state(State(known_albums={
        "active": _album("active", "Fresh Release", ACTIVE),
        "retired": _album("retired", "Ancient Release", RETIRED),
    }))

    assert connected_client.post(f"/albums/{album_id}/promote").status_code == 404


def test_promote_requires_a_spotify_connection(client, monkeypatch):
    core.save_config({**core.load_config(), "days_lookback": DAYS_LOOKBACK,
                      "spotify_playlist_id": "playlist123"})
    core.save_state(State(known_albums={"e": _album("e", "Aged Release", EXPIRED)}))
    monkeypatch.setattr(core, "is_connected", lambda: False)

    assert client.post("/albums/e/promote").status_code == 400


# --- Return to expired --------------------------------------------------------


def test_return_removes_tracks_and_clears_the_promotion(connected_client, offline_spotify):
    _, removed = offline_spotify
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED, added=True, override=False,
                    uris=["t:1", "t:2"]),
    }))

    response = connected_client.post("/albums/e/expire")

    assert response.status_code == 302
    assert removed == [("playlist123", ["t:1", "t:2"])]
    saved = core.load_state().known_albums["e"]
    assert saved.added_to_playlist is False
    assert saved.track_uris == []
    assert saved.manual_override is None


def test_return_requires_a_spotify_connection(client, monkeypatch):
    """Regression: an unconnected account used to raise AuthError (500) when
    the route called get_access_token unguarded."""
    core.save_config({**core.load_config(), "days_lookback": DAYS_LOOKBACK,
                      "spotify_playlist_id": "playlist123"})
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
    }))
    monkeypatch.setattr(core, "is_connected", lambda: False)

    def unreachable(*args, **kwargs):
        raise AssertionError("must not reach Spotify without a connection")

    monkeypatch.setattr(core, "get_access_token", unreachable)

    assert client.post("/albums/e/expire").status_code == 400


def test_return_without_a_playlist_configured_is_rejected(client, monkeypatch):
    core.save_config({**core.load_config(), "days_lookback": DAYS_LOOKBACK,
                      "spotify_playlist_id": ""})
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
    }))
    monkeypatch.setattr(core, "is_connected", lambda: True)

    def unreachable(*args, **kwargs):
        raise AssertionError("must not send a removal without a playlist id")

    monkeypatch.setattr(core, "remove_tracks_from_playlist", unreachable)

    response = client.post("/albums/e/expire")

    assert response.status_code == 400
    saved = core.load_state().known_albums["e"]
    assert saved.added_to_playlist is True


def test_return_keeps_state_intact_when_the_playlist_call_fails(connected_client, monkeypatch):
    monkeypatch.setattr(core, "is_connected", lambda: True)
    monkeypatch.setattr(core, "load_refresh_token", lambda: "refresh-token")

    def boom(*args, **kwargs):
        raise RuntimeError("spotify is down")

    monkeypatch.setattr(core, "get_access_token", boom)
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
    }))

    response = connected_client.post("/albums/e/expire")

    assert response.status_code == 502
    saved = core.load_state().known_albums["e"]
    assert saved.added_to_playlist is True
    assert saved.manual_override is False


def test_return_conflicts_while_a_scan_is_running(connected_client, offline_spotify):
    added, removed = offline_spotify
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
    }))

    assert core.run_lock.acquire(blocking=False)
    try:
        response = connected_client.post("/albums/e/expire")
    finally:
        core.run_lock.release()

    assert response.status_code == 409
    assert added == []
    assert removed == []
    assert core.load_state().known_albums["e"].added_to_playlist is True


def test_return_preserves_other_state_and_releases_the_lock(connected_client, offline_spotify):
    """The action must merge its change (not rewrite a stale whole-file
    snapshot) and must not leave run_lock held, which would block every
    later scan."""
    core.save_state(State(known_albums={
        "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
        "other": _album("other", "Fresh Release", ACTIVE, added=True, uris=["t:9"]),
    }))

    assert connected_client.post("/albums/e/expire").status_code == 302

    state = core.load_state()
    assert state.known_albums["e"].added_to_playlist is False
    assert state.known_albums["e"].manual_override is None
    # untouched album keeps its playlist state
    assert state.known_albums["other"].added_to_playlist is True
    assert state.known_albums["other"].track_uris == ["t:9"]
    assert not core.run_lock.locked()


@pytest.mark.parametrize("album_id", ["active", "retired", "missing"])
def test_return_rejects_albums_outside_the_expired_window(connected_client, offline_spotify,
                                                          album_id):
    core.save_state(State(known_albums={
        "active": _album("active", "Fresh Release", ACTIVE, added=True, uris=["t:1"]),
        "retired": _album("retired", "Ancient Release", RETIRED, added=True, uris=["t:1"]),
    }))

    assert connected_client.post(f"/albums/{album_id}/expire").status_code == 404