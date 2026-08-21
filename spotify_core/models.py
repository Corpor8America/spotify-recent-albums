"""Typed domain models for persisted state.

State on disk is JSON; these dataclasses are the in-memory representation.
``to_dict``/``from_dict`` keep the exact on-disk shape that previous
versions of the app wrote, so existing state files remain readable:

    {
      "artists": {"<artist_id>": {"name", "last_checked", "scanned_with"}},
      "known_albums": {"<album_id>": {...album fields...}},
      "in_progress": null | {"due_ids": [...], "processed_ids": [...]},
      "rate_limits": {"<category>": <unix ts>}
    }

The id of an album/artist is the dict key, so it is not serialized into
the value object itself.
"""

from dataclasses import dataclass, field
from typing import Optional

from .filters import is_auto_excluded


@dataclass
class Album:
    id: str
    name: str
    artist: str
    artist_id: str
    album_type: str
    release_date: str
    url: str
    total_tracks: int
    first_seen: str
    auto_excluded: bool = False
    manual_override: Optional[bool] = None
    added_to_playlist: bool = False
    track_uris: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, album_id, d):
        return cls(
            id=album_id,
            name=d.get("name", ""),
            artist=d.get("artist", ""),
            artist_id=d.get("artist_id", ""),
            album_type=d.get("type", ""),
            release_date=d.get("release_date", ""),
            url=d.get("url", ""),
            total_tracks=d.get("total_tracks", 0),
            first_seen=d.get("first_seen", ""),
            auto_excluded=bool(d.get("auto_excluded", False)),
            manual_override=d.get("manual_override"),
            added_to_playlist=bool(d.get("added_to_playlist", False)),
            track_uris=list(d.get("track_uris") or []),
        )

    def to_dict(self):
        return {
            "artist": self.artist,
            "artist_id": self.artist_id,
            "name": self.name,
            "type": self.album_type,
            "release_date": self.release_date,
            "url": self.url,
            "total_tracks": self.total_tracks,
            "first_seen": self.first_seen,
            "auto_excluded": self.auto_excluded,
            "manual_override": self.manual_override,
            "added_to_playlist": self.added_to_playlist,
            "track_uris": list(self.track_uris),
        }


@dataclass
class Artist:
    id: str
    name: str
    last_checked: str = ""
    scanned_with: str = ""

    @classmethod
    def from_dict(cls, artist_id, d):
        return cls(
            id=artist_id,
            name=d.get("name", artist_id),
            last_checked=d.get("last_checked", ""),
            scanned_with=d.get("scanned_with", ""),
        )

    def to_dict(self):
        return {
            "name": self.name,
            "last_checked": self.last_checked,
            "scanned_with": self.scanned_with,
        }


@dataclass
class ScanProgress:
    due_ids: list = field(default_factory=list)
    processed_ids: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        return cls(
            due_ids=list(d.get("due_ids") or []),
            processed_ids=list(d.get("processed_ids") or []),
        )

    def to_dict(self):
        return {"due_ids": list(self.due_ids), "processed_ids": list(self.processed_ids)}


@dataclass
class State:
    artists: dict = field(default_factory=dict)       # artist_id -> Artist
    known_albums: dict = field(default_factory=dict)  # album_id -> Album
    in_progress: Optional[ScanProgress] = None
    rate_limits: dict = field(default_factory=dict)   # category -> unix ts

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        in_progress = d.get("in_progress")
        return cls(
            artists={aid: Artist.from_dict(aid, entry)
                    for aid, entry in (d.get("artists") or {}).items()},
            known_albums={aid: Album.from_dict(aid, entry)
                          for aid, entry in (d.get("known_albums") or {}).items()},
            in_progress=ScanProgress.from_dict(in_progress) if in_progress else None,
            rate_limits={k: int(v) for k, v in (d.get("rate_limits") or {}).items()},
        )

    def to_dict(self):
        return {
            "artists": {aid: a.to_dict() for aid, a in self.artists.items()},
            "known_albums": {aid: a.to_dict() for aid, a in self.known_albums.items()},
            "in_progress": self.in_progress.to_dict() if self.in_progress else None,
            "rate_limits": dict(self.rate_limits),
        }
