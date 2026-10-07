"""SQLite storage for Spotify-only artist tracking.

One row per tracked artist per chat, plus the set of Spotify releases that have
already been announced. The file is ``spotify_radar.db``, so this project never
reads or writes the iTunes bot's ``itunes_radar.db``.

The schema is created empty on first run. There is no migration path from any
older database: artists are supplied by the administrator through ``/add`` or
``/bulkadd``, so a stale file is discarded rather than merged.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

SPOTIFY_PREFIX = "sp_"
FOREIGN_PREFIX = "it_"


def normalise_id(raw_id: Any) -> str:
    """Return the canonical ``sp_``-prefixed Spotify artist ID.

    An ID carrying the other platform's prefix is rejected rather than prefixed
    again, because such a value could never be queried against Spotify and would
    otherwise be stored as a subscription that silently never scans.
    """
    value = str(raw_id or "").strip()
    if not value or value.startswith(FOREIGN_PREFIX):
        return ""
    if value.startswith(SPOTIFY_PREFIX):
        return value
    return f"{SPOTIFY_PREFIX}{value}"


def strip_prefix(raw_id: Any) -> str:
    """Return the bare Spotify artist ID with any prefix removed."""
    value = str(raw_id or "").strip()
    if value.startswith(SPOTIFY_PREFIX):
        return value[len(SPOTIFY_PREFIX) :]
    return value


class Database:
    """SQLite interface for Spotify artist subscriptions and seen releases."""

    def __init__(self, db_path: str = "spotify_radar.db") -> None:
        self.db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        """Create the schema when absent.

        Any pre-existing table is rebuilt from scratch rather than migrated: the
        bot deliberately carries no history, so a database left over from an
        earlier run is emptied instead of being merged into the new one.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DROP TABLE IF EXISTS artists")
            conn.execute(
                """
                CREATE TABLE artists (
                    artist_key TEXT,
                    spotify_id TEXT,
                    name TEXT,
                    chat_id INTEGER,
                    PRIMARY KEY (artist_key, chat_id)
                )
                """
            )
            conn.execute("DROP TABLE IF EXISTS seen_releases")
            conn.execute(
                """
                CREATE TABLE seen_releases (
                    release_id TEXT PRIMARY KEY
                )
                """
            )
            # The notification destination is learned at run time rather than
            # configured, so it survives a restart and is never carried over.
            conn.execute("DROP TABLE IF EXISTS settings")
            conn.execute(
                """
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Artist CRUD
    # ------------------------------------------------------------------
    def add_artist(self, spotify_id: Any, name: str, chat_id: int) -> bool:
        """Insert a subscription.

        Returns ``True`` only when a new row is created; an artist that is
        already tracked in this chat is left untouched and reported as ``False``,
        so one artist is never scanned or alerted twice.
        """
        key = normalise_id(spotify_id)
        if not key:
            return False
        display_name = str(name or "").strip()

        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT 1 FROM artists WHERE artist_key = ? AND chat_id = ?",
                (key, chat_id),
            ).fetchone()
            if existing:
                return False
            try:
                conn.execute(
                    "INSERT INTO artists (artist_key, spotify_id, name, chat_id) "
                    "VALUES (?, ?, ?, ?)",
                    (key, key, display_name, chat_id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                return False
        return True

    def remove_artist(self, artist_id: Any, chat_id: int) -> int:
        """Remove a subscription by either prefixed or bare artist ID."""
        key = str(artist_id or "").strip()
        candidates = sorted({key, normalise_id(key)} - {""})
        if not candidates:
            return 0
        placeholders = ",".join("?" * len(candidates))
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                f"DELETE FROM artists WHERE artist_key IN ({placeholders}) AND chat_id = ?",
                (*candidates, chat_id),
            )
            conn.commit()
            return cursor.rowcount

    def get_user_artists(self, chat_id: int) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT name FROM artists WHERE chat_id = ? ORDER BY name COLLATE NOCASE",
                (chat_id,),
            ).fetchall()
        unique: list[str] = []
        seen: set[str] = set()
        for (name,) in rows:
            key = str(name or "").strip().casefold()
            if key and key not in seen:
                seen.add(key)
                unique.append(name)
        return unique

    def get_artist_rows(self) -> list[dict[str, Any]]:
        """Return every subscription as ``(artist_key, spotify_id, name, chat_id)``."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT artist_key, spotify_id, name, chat_id FROM artists ORDER BY rowid"
            ).fetchall()
        return [
            {
                "artist_key": key or "",
                "spotify_id": spotify_id or "",
                "name": name,
                "chat_id": chat_id,
            }
            for key, spotify_id, name, chat_id in rows
        ]

    # ------------------------------------------------------------------
    # Seen-release tracking
    # ------------------------------------------------------------------
    def is_release_seen(self, release_id: str) -> bool:
        with closing(self._connect()) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM seen_releases WHERE release_id = ?",
                    (str(release_id),),
                ).fetchone()
                is not None
            )

    def mark_release_seen(self, release_id: str) -> None:
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_releases (release_id) VALUES (?)",
                    (str(release_id),),
                )
                conn.commit()
        except sqlite3.IntegrityError:
            pass

    # ------------------------------------------------------------------
    # Runtime settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (str(key),)
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else ""

    def set_setting(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (str(key), str(value)),
            )
            conn.commit()

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)