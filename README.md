# Telegram Release Radar — Spotify

A standalone Telegram bot that watches tracked artists on **Spotify only** and
alerts on new albums, singles and guest appearances released in the last five
days. It shares no code and no database with the iTunes bot in `../iTunes/`, so
the two can be deployed, restarted or deleted independently.

## Files

| File | Purpose |
| --- | --- |
| `spotify_bot.py` | Telegram command handlers and the daily 05:35 IST scan |
| `spotify_database.py` | SQLite storage for subscriptions, seen releases and settings |
| `spotify_music_client.py` | Spotify Web API client, auth, rate limiting, filtering, dedup |
| `spotify_radar.db` | Local database (the server keeps its own copy) |

There is no auth-setup helper and no backfill tool: authentication needs only a
client id and secret from the dashboard, and the artist list is entered by hand.

## Configuration

Everything lives in `.env`, which is never committed:

```env
BOT_TOKEN=123456789:AA...            # from @BotFather
SPOTIFY_CLIENT_ID=5a4084...          # from the Spotify developer dashboard
SPOTIFY_CLIENT_SECRET=6f2b91...       # from the Spotify developer dashboard
```

`BOT_TOKEN` must be a token created for **this** bot with @BotFather. Two bots
polling with the same token both receive `409 Conflict` and every command
silently fails, so do not reuse the iTunes bot's token.

`SPOTIFY_CLIENT_SECRET` is the sensitive value, so treat it like a password and
keep it out of Telegram, screenshots and chat.

Neither the chat ID nor the scan time is configured. Alerts go to whichever
account sends the bot a private `/start` first, and the scan always runs at
05:35 IST. Anything already set as a real environment variable on the server
overrides `.env`.

## Authentication

The bot uses the **client-credentials grant and nothing else**:

```http
POST https://accounts.spotify.com/api/token
Authorization: Basic base64(SPOTIFY_CLIENT_ID:SPOTIFY_CLIENT_SECRET)
grant_type=client_credentials
```

The returned app-only access token is cached until shortly before it expires.
A `401` triggers exactly one new token fetch and one retry.

There is **no refresh token, no PKCE flow and no user login**: the bot only reads
public catalogue data, so it never acts on behalf of a Spotify account. If a
`SPOTIFY_REFRESH_TOKEN` is present in the environment it is ignored.

Check the credentials before deploying — `/status` should show `spotify` as
ONLINE and `spotify_albums` as ONLINE.

## Run

```bash
python3 -m venv .venv
.venv/bin/activate
pip install -r requirements.txt
python spotify_bot.py
```

## Scheduling

The scan is registered with `application.job_queue.run_daily` at **05:35
Asia/Kolkata**. The time is timezone-aware, so it fires at 05:35 in India no
matter where the server is hosted. Spotify meters quota per app, so one pass a
day keeps request volume far below the limit.

## Commands

- `/start` — help; in a private chat it also claims the alert destination
- `/add <artist name or link>` — track an artist on Spotify
- `/bulkadd <names or links>` — track several artists at once
- `/list` — list tracked artists
- `/remove <artist name or link>` — stop tracking
- `/bulkremove <names>` — stop tracking several artists
- `/status` — token health, release-discovery health, tracked count, next scan
- `/id` — show where alerts are sent and your own user/chat IDs
- `/pause` / `/resume` — pause or resume the daily scan

A `.txt`, `.csv` or `.json` file can be uploaded to import a list of artists.

## Alert destination

Telegram only lets a bot message someone who has started it, so the destination
cannot be hardcoded from a token alone. The **first account to send a private
`/start`** is recorded in the `settings` table of `spotify_radar.db` and becomes
the alert target; later users are told where alerts already go. Run `/id` to see
the current destination, or delete the `admin_chat_id` row to reassign it.

Because this is claimed rather than configured, anyone with the bot token can
take the destination. Keep the token private.

## What gets alerted

Each scan collects two kinds of result for every tracked artist:

- **Own releases** from `/artists/{id}/albums?include_groups=album,single`.
- **Features** from `/artists/{id}/albums?include_groups=appears_on`. Each
  candidate album is opened with `/albums/{id}`, because `appears_on` names the
  record the appearance sits on, not the track itself or the act that owns it.
  The guest track is then reported on its own, naming the lead act.

A release is only reported when all of the following hold:

- its day-precision release date falls within `RELEASE_LOOKBACK_DAYS` (default 5)
  and is not in the future;
- the tracked artist is credited exactly, compared on normalised names only —
  never a substring match;
- the credit is a real credit, so nothing credited to `Various Artists` passes;
- the title is not a third-party version or a set: remix, mashup, bootleg,
  rework, edit, dub, VIP, flip, slowed/sped-up/reverb copies, type beat, cover,
  remaster, compilation, DJ mix, continuous mix, live set, karaoke, tribute or
  instrumental;
- the release is not a Spotify `compilation` album.

When the tracked artist is not the first credit, the alert is a feature and
reads `Artist: <lead act>` / `Featuring: <tracked artist>`, so the notification
names who is actually featured rather than reporting a remix of their track.

## Filtering and reliability

- Access tokens are cached and fetched with `grant_type=client_credentials`.
- Transient failures retry with bounded exponential backoff. A `429` is **not**
  retried, because retrying only deepens the app-level lockout; the client then
  pauses itself for `SPOTIFY_QUOTA_COOLDOWN_SECONDS`.
- Seen releases are keyed per destination chat, so a rescan never re-sends an
  alert, and a fresh database means the first scan reports everything still
  inside the five-day window.
- Deduplication is by lead act + normalised title + release date, so the same
  album returned by several requests produces one alert.

## Optional tuning

All have working defaults and none need to be set: `DATABASE_PATH`,
`RELEASE_LOOKBACK_DAYS`, `MUSIC_MAX_RETRIES`, `SPOTIFY_CHECK_DELAY_SECONDS`,
`SPOTIFY_MARKET`, `IMPORT_DELAY_SECONDS`, `SPOTIFY_QUOTA_COOLDOWN_SECONDS`,
`MAX_IMPORT_ARTISTS`, `MAX_IMPORT_BYTES`.

## Database

`spotify_radar.db` holds tracked artists, already-announced releases and the
claimed alert destination. It is created empty and rebuilt empty on every start:
there is no migration from an older database and no import path, because the
artist list is entered by hand through the bot. Delete the file to start over.