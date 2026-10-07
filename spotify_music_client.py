"""Spotify release discovery through the Web API.

Authentication uses the client-credentials grant and nothing else: an app-only
access token is obtained from ``SPOTIFY_CLIENT_ID`` and ``SPOTIFY_CLIENT_SECRET``
and cached until shortly before it expires, then fetched again if Spotify answers
``401``. No refresh token, PKCE flow or user authorisation exists anywhere in this
module, because release discovery only needs public catalogue data.

Both values, together with ``BOT_TOKEN``, are read from ``.env`` next to the
bot. A real environment variable always takes precedence over the file.

Requests are rate-limited with bounded exponential backoff, and Spotify's
app-level quota error is never retried because retrying only deepens the lockout;
the engine then pauses itself for ``SPOTIFY_QUOTA_COOLDOWN_SECONDS``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

import requests
from requests import Response
from requests.exceptions import RequestException
from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)

# Load .env here as well as in the entry point, so this module is usable on its
# own. override=False keeps real environment variables ahead of the file.
load_dotenv()

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_URL = "https://api.spotify.com/v1"
PLATFORM_SPOTIFY = "spotify"
SPOTIFY_PREFIX = "sp_"

# Spotify's current build rejects limit=50 on /artists/{id}/albums with
# HTTP 400 "Invalid limit"; 20 is the highest value the endpoint still accepts.
SPOTIFY_ALBUMS_PAGE_LIMIT = 20

# Stable public artist used only to probe whether the albums endpoint answers.
SPOTIFY_PROBE_ARTIST_ID = "1uNFoZAHBGtllmzznpCI3s"

TRANSIENT_HTTP_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}
STATUS_KEYS = ("status", "log", "details")


class MusicServiceError(RuntimeError):
    """A service failed in a way that should not abort the scan."""


class QuotaExceededError(MusicServiceError):
    """Spotify refused further work; stop the current scan instead of retrying."""


def strip_prefix(raw_id: Any) -> str:
    """Return the bare Spotify artist ID with any prefix removed."""
    value = str(raw_id or "").strip()
    if value.startswith(SPOTIFY_PREFIX):
        return value[len(SPOTIFY_PREFIX) :]
    return value


class SpotifyMusicEngine:
    """Discover recently released albums, singles and features on Spotify.

    ``SPOTIFY_PROXY_URL`` can point to a legitimate egress proxy for Spotify's
    token and Web API requests.
    """

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        spotify_proxy_url: Optional[str] = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        recent_days: int = 5,
        max_retries: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            }
        )
        self.timeout = timeout
        self.recent_days = max(1, int(recent_days))
        self.max_retries = max(1, int(max_retries))

        proxy_url = spotify_proxy_url or os.getenv("SPOTIFY_PROXY_URL", "").strip()
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._artist_cache: dict[str, tuple[str, str]] = {}
        self._quota_until = 0.0
        self._quota_cooldown = max(
            60, int(os.getenv("SPOTIFY_QUOTA_COOLDOWN_SECONDS", "900").strip() or 900)
        )

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_html(text: str) -> str:
        """Return a compact, safe-to-log summary of an HTML/error response."""
        without_tags = re.sub(r"<[^>]+>", " ", text or "")
        return " ".join(without_tags.split())[:180]

    @staticmethod
    def _normalise_text(value: Any) -> str:
        """Normalise text for equality comparisons without relying on substrings."""
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(char for char in text if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "", text.casefold())

    @staticmethod
    def _clean_display_title(title: Any) -> str:
        """Drop the store's collection-type suffix from a shown title."""
        return (
            re.sub(
                r"\s*[-–—]\s*(?:single|ep|album)\s*$", "", str(title or ""), flags=re.I
            ).strip()
            or str(title or "").strip()
        )

    @classmethod
    def _normalise_title(cls, title: Any) -> str:
        """Normalise harmless store-specific title decorations for deduplication."""
        text = str(title or "")
        text = re.sub(
            r"\s*[\(\[\{][^\]\)\}]*\b(?:feat(?:uring)?|ft\.)\b[^\]\)\}]*[\]\)\}]",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*[-–—]\s*(?:single|ep)\s*$", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\s*[\(\[\{]\s*(?:deluxe|expanded|anniversary|remaster(?:ed)?|"
            r"special)\s+(?:edition|version)?\s*[\]\)\}]\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return cls._normalise_text(text)

    @staticmethod
    def _parse_release_date(value: Any) -> Optional[date]:
        """Parse only day-precision dates, which are required by the lookback rule."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            return None
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None

    def _is_recent(self, release_date: Any) -> bool:
        parsed = self._parse_release_date(release_date)
        if not parsed:
            return False
        today = date.today()
        return today - timedelta(days=self.recent_days) <= parsed <= today

    @staticmethod
    def _format_date(value: Any) -> Optional[str]:
        parsed = SpotifyMusicEngine._parse_release_date(value)
        return parsed.isoformat() if parsed else None

    @staticmethod
    def _error_from_response(response: Response) -> str:
        body = SpotifyMusicEngine._clean_html(response.text)
        return f"HTTP {response.status_code}" + (f": {body}" if body else "")

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Response:
        """Make a bounded request, retrying only transient failures.

        A ``429`` here is Spotify's hard, app-level quota, so it raises
        ``QuotaExceededError`` instead of retrying: another attempt only extends
        the lockout.
        """
        last_error: Optional[str] = None
        request_kwargs = dict(kwargs)
        request_kwargs.setdefault("timeout", self.timeout)
        request_kwargs.setdefault("params", params)
        if self.proxies:
            request_kwargs["proxies"] = self.proxies

        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, **request_kwargs)
            except RequestException as exc:
                last_error = f"connection error: {str(exc)[:160]}"
            else:
                if response.status_code == 200:
                    return response
                last_error = self._error_from_response(response)
                if response.status_code not in TRANSIENT_HTTP_STATUSES:
                    break
                if response.status_code == 429:
                    raise QuotaExceededError(last_error)

                retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        min(float(retry_after), 10.0)
                        if retry_after
                        else 0.5 * (2**attempt)
                    )
                except ValueError:
                    delay = 0.5 * (2**attempt)
                # Longer backoff for 403 (often temporary IP blocks on shared hosting)
                if response.status_code == 403:
                    delay = max(delay, 2.0 * (2**attempt))
                time.sleep(min(delay, 15.0))
                continue

            if attempt < self.max_retries - 1:
                time.sleep(0.5 * (2**attempt))

        raise MusicServiceError(last_error or "request failed without an error message")

    @staticmethod
    def _json(response: Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MusicServiceError("Spotify returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MusicServiceError("Spotify returned an unexpected JSON payload")
        return payload

    # ------------------------------------------------------------------
    # Strict false-positive filtering and deduplication
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_non_release(title: str) -> bool:
        """Reject catalogue entries that are not the artist's own new material.

        Third-party versions of a record — remixes, mashups, bootlegs, reworks,
        sped-up/slowed copies — carry the original artist's name but are not a new
        release, so they are dropped by title. Sets, compilations and live audio
        are dropped for the same reason.
        """
        folded = str(title or "").casefold()
        patterns = (
            # Third-party versions and re-uploads.
            r"\bremix(?:ed)?\b",
            r"\bremixes\b",
            r"\bmashup\b",
            r"\bbootleg\b",
            r"\brework(?:ed)?\b",
            r"\bdub\b",
            r"\bvip\b",
            r"\bedit(?:ed)?\b",
            r"\bflips?\b",
            r"\b(?:slowed|sped\s*up|reverb|8d|bass[ _-]?boosted)\b",
            r"\btype\s*beat\b",
            r"\bcover(?:ed)?\b",
            r"\bremaster(?:ed)?\b",
            # Sets, compilations and live audio.
            r"\bvarious artists\b",
            r"\bcompilation\b",
            r"\bcontinuous\s+(?:mix|set)\b",
            r"\b(?:total|mega|non[ _-]?stop)\s+mix\b",
            r"\bdj\s*(?:mix|set)\b",
            r"\b(?:mix|set)\s+by\s+dj\b",
            r"\b(?:live|concert)\s+(?:at|from|in)\b",
            r"\blive\s+(?:set|session|performance)\b",
            r"\bsoundcheck\b",
            r"\bkaraoke\b",
            r"\btribute\b",
            r"\binstrumental\b",
        )
        return any(re.search(pattern, folded) for pattern in patterns)

    # Credits arrive as display strings, so "A, B & C", "A feat. B" and
    # "A x B" all have to be split before the tracked artist can be located.
    CREDIT_SPLIT_PATTERN = re.compile(
        r"\s*(?:,|;|&|/|\+|\bx\b|\band\b|\bfeat(?:uring)?\.?\b|\bft\.?\b|\bwith\b"
        r"|\bvs\.?\b|\bversus\b)\s*",
        flags=re.IGNORECASE,
    )

    @classmethod
    def _credit_names(cls, credited_artists: Iterable[Any]) -> list[str]:
        """Split raw credit strings into individual, de-duplicated artist names."""
        names: list[str] = []
        for credit in credited_artists:
            raw = str(credit or "").strip()
            if not raw:
                continue
            for part in cls.CREDIT_SPLIT_PATTERN.split(raw):
                name = part.strip(" \t-–—")
                if name and name not in names:
                    names.append(name)
        return names

    @classmethod
    def _credit_index(cls, artist_name: str, credits: list[str]) -> Optional[int]:
        """Return where the tracked artist sits in the credit list, or ``None``.

        The position decides how the release is reported: index 0 is the act's
        own record, anything later is a feature on someone else's.
        """
        target = cls._normalise_text(artist_name)
        if not target:
            return None
        for index, credit in enumerate(credits):
            if cls._normalise_text(credit) == target:
                return index
        return None

    @staticmethod
    def _is_various_artists(credits: Iterable[str]) -> bool:
        """A store 'Various Artists' credit can never be a real collaboration."""
        return any(
            SpotifyMusicEngine._normalise_text(credit).startswith("variousartists")
            for credit in credits
        )

    def _make_release(
        self,
        *,
        source_id: str,
        artist_name: str,
        credited_artists: Iterable[Any],
        name: Any,
        release_type: Any,
        release_date: Any,
        url: Any,
    ) -> Optional[dict[str, Any]]:
        """Build one alert record, or ``None`` when the entry must be dropped."""
        title = str(name or "").strip()
        release_day = self._format_date(release_date)
        kind = str(release_type or "").casefold()
        credits = self._credit_names(credited_artists)
        credit_index = self._credit_index(artist_name, credits)

        if (
            not source_id
            or not title
            or not release_day
            or not self._is_recent(release_day)
            or credit_index is None
            or self._is_various_artists(credits)
            or self._looks_like_non_release(title)
            or kind == "compilation"
        ):
            return None

        clean_title = self._normalise_title(title)
        if not clean_title:
            return None

        # The first credit is the act the release is sold as; every later credit
        # is a guest, which for the tracked artist means a feature appearance.
        is_feature = credit_index > 0
        primary_artist = credits[0] if credits else artist_name
        others = [credit for credit in credits[1:]]

        # A deterministic content identity prevents duplicates on future rescans.
        material = (
            f"{self._normalise_text(primary_artist)}|{clean_title}|{release_day}"
        ).encode("utf-8")
        dedup_key = "release_" + hashlib.sha256(material).hexdigest()[:24]
        return {
            "id": dedup_key,
            "dedup_key": dedup_key,
            "source_ids": [source_id],
            "platforms": [PLATFORM_SPOTIFY],
            "name": self._clean_display_title(title),
            "type": "feature" if is_feature else (
                "album" if kind == "album" else "single"
            ),
            "release_date": release_day,
            "url": str(url or ""),
            "artist_name": primary_artist,
            "credited_artists": others,
            "is_feature": is_feature,
        }

    def _merge_releases(
        self, releases: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Collapse same-title/same-date records into one alert."""
        merged: dict[str, dict[str, Any]] = {}
        for release in releases:
            key = str(release["dedup_key"])
            existing = merged.get(key)
            if not existing:
                merged[key] = dict(release)
                continue
            for source_id in release.get("source_ids", []):
                if source_id not in existing["source_ids"]:
                    existing["source_ids"].append(source_id)
            if release.get("type") == "album":
                existing["type"] = "album"

        return sorted(
            merged.values(),
            key=lambda release: (release["release_date"], release["name"].casefold()),
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Authentication: client id + client secret, never a refresh token
    # ------------------------------------------------------------------
    @staticmethod
    def _configured_credentials() -> tuple[str, str]:
        """Return ``(client_id, client_secret)`` from the environment."""
        return (
            os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
            os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
        )

    def _get_spotify_token(
        self, *, force_refresh: bool = False
    ) -> tuple[Optional[str], str]:
        """Fetch an app-only access token with the client-credentials grant.

        The bot reads public catalogue data, so it never acts on behalf of a
        Spotify account: there is no refresh token to store, rotate or revoke.
        """
        if (
            not force_refresh
            and self._token
            and time.monotonic() < self._token_expires_at
        ):
            return self._token, "cached client-credentials access token"

        client_id, client_secret = self._configured_credentials()
        if not client_id:
            return None, "Missing SPOTIFY_CLIENT_ID"
        if not client_secret:
            return None, "Missing SPOTIFY_CLIENT_SECRET"

        try:
            response = self._request(
                "POST",
                SPOTIFY_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = self._json(response)
            token = payload.get("access_token")
            expires_in = payload.get("expires_in", 3600)
            if not isinstance(token, str) or not token:
                raise MusicServiceError(
                    "Spotify token response did not contain access_token"
                )
            try:
                ttl = max(60, int(expires_in) - 60)
            except (TypeError, ValueError):
                ttl = 3300
            self._token = token
            self._token_expires_at = time.monotonic() + ttl
            return token, "client-credentials access token obtained"
        except MusicServiceError as exc:
            return None, str(exc)

    def _spotify_get(
        self, path: str, *, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """GET a Spotify endpoint, obtaining a fresh token once on a ``401``."""
        for refresh in (False, True):
            token, reason = self._get_spotify_token(force_refresh=refresh)
            if not token:
                raise MusicServiceError(reason)
            try:
                response = self._request(
                    "GET",
                    f"{SPOTIFY_API_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except MusicServiceError as exc:
                if "HTTP 401" in str(exc) and not refresh:
                    self._token = None
                    self._token_expires_at = 0.0
                    continue
                raise
            return self._json(response)
        raise MusicServiceError("Spotify authentication failed after token refresh")

    # ------------------------------------------------------------------
    # Artist resolution and release discovery
    # ------------------------------------------------------------------
    def _spotify_artist(self, artist_id: str, artist_name: str) -> tuple[str, str]:
        cache_key = f"{artist_id}|{self._normalise_text(artist_name)}"
        if cache_key in self._artist_cache:
            return self._artist_cache[cache_key]

        raw_id = strip_prefix(artist_id)
        if raw_id:
            payload = self._spotify_get(
                f"/artists/{urllib.parse.quote(raw_id, safe='')}"
            )
            resolved_id, resolved_name = str(payload.get("id", "")), str(
                payload.get("name", "")
            )
        else:
            payload = self._spotify_get(
                "/search",
                params={"q": artist_name, "type": "artist", "limit": 10},
            )
            candidates = payload.get("artists", {}).get("items", [])
            target = self._normalise_text(artist_name)
            exact = next(
                (
                    item
                    for item in candidates
                    if self._normalise_text(item.get("name")) == target
                ),
                None,
            )
            item = exact or (candidates[0] if candidates else {})
            resolved_id, resolved_name = str(item.get("id", "")), str(
                item.get("name", "")
            )
        if not resolved_id or not resolved_name:
            raise MusicServiceError("Spotify could not resolve the artist")
        self._artist_cache[cache_key] = (resolved_id, resolved_name)
        return resolved_id, resolved_name

    @staticmethod
    def _is_invalid_limit_error(exc: MusicServiceError) -> bool:
        message = str(exc)
        return "HTTP 400" in message and "invalid limit" in message.casefold()

    def _album_page(
        self,
        spotify_id: str,
        limit: int,
        offset: int,
        *,
        include_groups: str = "album,single",
    ) -> list[dict[str, Any]]:
        """Fetch one albums page, halving the page size if Spotify rejects it."""
        current = limit
        while True:
            try:
                payload = self._spotify_get(
                    f"/artists/{urllib.parse.quote(spotify_id, safe='')}/albums",
                    params={
                        "include_groups": include_groups,
                        "limit": current,
                        "offset": offset,
                    },
                )
            except MusicServiceError as exc:
                if self._is_invalid_limit_error(exc) and current > 1:
                    current = max(1, current // 2)
                    LOGGER.warning(
                        "Spotify rejected limit=%d; retrying with limit=%d",
                        current * 2,
                        current,
                    )
                    continue
                raise
            page = payload.get("items", [])
            if not isinstance(page, list):
                raise MusicServiceError("Spotify returned an invalid albums list")
            return [item for item in page if isinstance(item, dict)]

    def _album_pages(self, spotify_id: str, include_groups: str) -> list[dict[str, Any]]:
        """Walk an artist's catalogue for one group of releases."""
        items: list[dict[str, Any]] = []
        page_limit = SPOTIFY_ALBUMS_PAGE_LIMIT
        offset = 0
        while offset < 200:
            page = self._album_page(
                spotify_id, page_limit, offset, include_groups=include_groups
            )
            items.extend(page)
            if len(page) < page_limit:
                break
            offset += len(page)
        return items

    @property
    def spotify_market(self) -> str:
        """Market used for album lookups, because an app-only token has no user."""
        return os.getenv("SPOTIFY_MARKET", "US").strip() or "US"

    def _own_releases(self, spotify_id: str, spotify_name: str) -> list[dict[str, Any]]:
        """Return the artist's own albums and singles released recently."""
        releases: list[dict[str, Any]] = []
        for item in self._album_pages(spotify_id, "album,single"):
            artists = [
                artist
                for artist in item.get("artists", [])
                if isinstance(artist, dict)
            ]
            artist_ids = {str(artist.get("id", "")) for artist in artists}
            if spotify_id not in artist_ids:
                continue
            credits = [artist.get("name", "") for artist in artists]
            release = self._make_release(
                source_id=f"{SPOTIFY_PREFIX}{item.get('id', '')}",
                artist_name=spotify_name,
                credited_artists=credits,
                name=item.get("name"),
                release_type=item.get("album_type") or item.get("album_group"),
                release_date=item.get("release_date"),
                url=(item.get("external_urls") or {}).get("spotify", ""),
            )
            if release:
                releases.append(release)
        return releases

    def _album_features(
        self, album_id: str, spotify_id: str, spotify_name: str
    ) -> list[dict[str, Any]]:
        """Return the guest tracks the artist appears on inside one album.

        The album is fetched in full because ``appears_on`` only names the record
        the appearance sits on, not the track or the act that owns it. Compilations
        are skipped outright: a shared release is somebody else's, and every
        contributor is a "Various Artists" entry rather than a real credit.
        """
        try:
            payload = self._spotify_get(
                f"/albums/{urllib.parse.quote(album_id, safe='')}",
                params={"limit": 50, "market": self.spotify_market},
            )
        except MusicServiceError as exc:
            LOGGER.info("Could not read Spotify album %s: %s", album_id, exc)
            return []

        if str(payload.get("album_type", "")).casefold() == "compilation":
            return []
        release_day = self._format_date(payload.get("release_date"))
        if not release_day or not self._is_recent(release_day):
            return []

        album_credits = [
            artist.get("name", "")
            for artist in payload.get("artists", [])
            if isinstance(artist, dict)
        ]
        if self._is_various_artists(album_credits):
            return []

        tracks = [
            track
            for track in payload.get("items", [])
            if isinstance(track, dict)
        ]
        releases: list[dict[str, Any]] = []
        for track in tracks:
            artists = [
                artist
                for artist in track.get("artists", [])
                if isinstance(artist, dict)
            ]
            if spotify_id not in {str(artist.get("id", "")) for artist in artists}:
                continue
            release = self._make_release(
                source_id=f"{SPOTIFY_PREFIX}{track.get('id', '')}",
                artist_name=spotify_name,
                credited_artists=[artist.get("name", "") for artist in artists],
                name=track.get("name"),
                release_type="single",
                release_date=release_day,
                url=(track.get("external_urls") or {}).get("spotify", ""),
            )
            # Only genuine guest appearances are features here; the artist being
            # the act the album belongs to is reported by _own_releases instead.
            if release and release["is_feature"]:
                releases.append(release)
        return releases

    def _features(self, spotify_id: str, spotify_name: str) -> list[dict[str, Any]]:
        """Return tracks the artist features on other people's records."""
        releases: list[dict[str, Any]] = []
        for item in self._album_pages(spotify_id, "appears_on"):
            album_id = str(item.get("id") or "").strip()
            if not album_id:
                continue
            release_day = self._format_date(item.get("release_date"))
            if not release_day or not self._is_recent(release_day):
                continue
            if self._looks_like_non_release(str(item.get("name") or "")):
                continue
            releases.extend(self._album_features(album_id, spotify_id, spotify_name))
        return releases

    def _releases(self, artist_id: str, artist_name: str) -> list[dict[str, Any]]:
        """Return recent own releases plus recent guest appearances."""
        spotify_id, spotify_name = self._spotify_artist(artist_id, artist_name)
        return self._own_releases(spotify_id, spotify_name) + self._features(
            spotify_id, spotify_name
        )

    # ------------------------------------------------------------------
    # Public API used by spotify_bot.py
    # ------------------------------------------------------------------
    @staticmethod
    def _id_from_query(query: str) -> Optional[str]:
        # Spotify prefixes some artist links with a path segment, e.g.
        # open.spotify.com/intl-de/artist/<id>, which must still resolve.
        match = re.search(
            r"(?:open\.spotify\.com/(?:[^/?#]+/)?artist/"
            r"|spotify:artist:)([A-Za-z0-9]+)",
            str(query or ""),
            re.I,
        )
        return match.group(1) if match else None

    @staticmethod
    def detect_platform(query: str) -> Optional[str]:
        """Return ``'spotify'`` when the query is a Spotify link."""
        if re.search(r"(?:open\.spotify\.com|spotify:)", str(query or ""), re.I):
            return PLATFORM_SPOTIFY
        return None

    def get_artist_info(self, query: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Resolve an artist on Spotify. Returns ``(artist_dict, error)``."""
        query = str(query or "").strip()
        if not query:
            return None, "Empty query"
        spotify_id = self._id_from_query(query)
        try:
            resolved_id, name = self._spotify_artist(
                f"{SPOTIFY_PREFIX}{spotify_id}" if spotify_id else "", query
            )
            return {"id": resolved_id, "name": name}, None
        except MusicServiceError as exc:
            LOGGER.info("Spotify artist resolution failed for %r: %s", query, exc)
            return None, str(exc)

    def check_spotify_releases(
        self, artist_id: str, artist_name: str = ""
    ) -> list[dict[str, Any]]:
        """Return recent Spotify releases for an artist, newest first.

        Failures are logged and return an empty list so one bad artist cannot
        stop the rest of the scan.
        """
        if not artist_id:
            return []
        if time.monotonic() < self._quota_until:
            LOGGER.info(
                "Skipping Spotify for %s; quota cooldown still active",
                artist_name or artist_id,
            )
            return []
        try:
            return self._merge_releases(self._releases(artist_id, artist_name))
        except QuotaExceededError as exc:
            self._quota_until = time.monotonic() + self._quota_cooldown
            LOGGER.warning(
                "Spotify app quota exhausted (%s); pausing scanning for %ds.",
                exc,
                self._quota_cooldown,
            )
            return []
        except MusicServiceError as exc:
            LOGGER.warning(
                "Spotify release scan failed for %s: %s",
                artist_name or artist_id,
                exc,
            )
            return []
        except Exception:
            LOGGER.exception(
                "Unexpected Spotify release scan failure for %s",
                artist_name or artist_id,
            )
            return []

    @staticmethod
    def _status(status: str, message: str) -> dict[str, str]:
        """Return the exact schema consumed by spotify_bot.py's /status formatter."""
        return {"status": status, "log": message, "details": message}

    def get_status_report(self) -> dict[str, dict[str, str]]:
        """Report authentication and release-discovery health."""
        report: dict[str, dict[str, str]] = {}

        token, token_message = self._get_spotify_token()
        if not token:
            report[PLATFORM_SPOTIFY] = self._status("ERROR", token_message)
        else:
            try:
                self._spotify_get(
                    "/search", params={"q": "test", "type": "artist", "limit": 1}
                )
                proxy_note = " via configured proxy" if self.proxies else ""
                report[PLATFORM_SPOTIFY] = self._status(
                    "ONLINE",
                    f"{token_message} (HTTP 200){proxy_note}",
                )
            except MusicServiceError as exc:
                report[PLATFORM_SPOTIFY] = self._status("ERROR", str(exc))

        # Authentication being healthy does not mean release discovery works, so
        # probe the albums endpoint the scanner actually depends on.
        cached = next(iter(self._artist_cache.values()), ("", ""))
        probe_id = cached[0] or SPOTIFY_PROBE_ARTIST_ID
        try:
            self._album_page(probe_id, 1, 0)
        except QuotaExceededError as exc:
            report["spotify_albums"] = self._status(
                "WARNING",
                f"Release discovery blocked by app quota ({str(exc)[:150]}). "
                "Alerts pause until the cooldown ends.",
            )
        except MusicServiceError as exc:
            report["spotify_albums"] = self._status("WARNING", str(exc))
        else:
            report["spotify_albums"] = self._status(
                "ONLINE", "Release discovery reachable"
            )

        report[PLATFORM_SPOTIFY] = {
            key: str(report[PLATFORM_SPOTIFY].get(key, "")) for key in STATUS_KEYS
        }
        return report