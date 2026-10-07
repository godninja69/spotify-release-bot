"""Telegram command handlers and the daily Spotify release scan.

This bot tracks artists on Spotify only and sends one Telegram alert per new
album, single or guest appearance released in the last five days. It starts from
an empty database: artists arrive through ``/add``, ``/bulkadd`` or a file
upload, and there is no stored history to replay. The scan runs through
``application.job_queue.run_daily`` at 05:35 IST; there is no polling loop and no
long-running timer.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import spotify_database as db
import spotify_music_client as music_client

# Do not overwrite real deployment environment variables with a local .env file.
load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
LOGGER = logging.getLogger(__name__)

SCAN_JOB_NAME = "spotify-daily-scan"

# The scan runs once a day at 05:35 India Standard Time, independent of the
# server's own time zone. IST has no daylight saving, so the offset is stable.
SCAN_TIMEZONE = ZoneInfo("Asia/Kolkata")
SCAN_TIME = dt_time(5, 35, tzinfo=SCAN_TIMEZONE)

# Alerts go to the bot administrator's own account, learned from the first
# private /start and persisted, so no chat ID has to be configured.
ADMIN_CHAT_KEY = "admin_chat_id"


def _int_setting(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(minimum, int(raw))
    except ValueError:
        LOGGER.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


def _float_setting(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        return max(minimum, float(raw))
    except ValueError:
        LOGGER.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


store = db.Database(os.getenv("DATABASE_PATH", "spotify_radar.db"))
music = music_client.SpotifyMusicEngine(
    recent_days=_int_setting("RELEASE_LOOKBACK_DAYS", 5, 1),
    max_retries=_int_setting("MUSIC_MAX_RETRIES", 3, 1),
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MAX_IMPORT_ARTISTS = _int_setting("MAX_IMPORT_ARTISTS", 200, 1)
MAX_IMPORT_BYTES = _int_setting("MAX_IMPORT_BYTES", 1048576, 1)

# Pacing between artists inside one scan; Spotify meters quota per app.
CHECK_DELAY_SECONDS = _float_setting("SPOTIFY_CHECK_DELAY_SECONDS", 1.5, 0.2)

SCAN_STATE: dict[str, Any] = {"last": "", "sent": 0, "found": 0}


def admin_chat_id() -> int:
    """Return the administrator account alerts are delivered to, or 0."""
    raw = store.get_setting(ADMIN_CHAT_KEY).strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def _claim_admin(update: Update) -> bool:
    """Record the first private /start as the notification destination.

    Telegram only lets a bot message someone who has started it, so the admin
    account has to be learned rather than configured. The first private chat to
    run /start wins; anyone later is told where the alerts already go.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type != "private" or not user:
        return False
    existing = admin_chat_id()
    if existing:
        return existing == chat.id
    store.set_setting(ADMIN_CHAT_KEY, str(chat.id))
    LOGGER.warning(
        "Alerts will now be delivered to admin account %s, claimed via /start.",
        chat.id,
    )
    return True


async def run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking HTTP client work off python-telegram-bot's event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: function(*args, **kwargs))


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _safe_http_url(value: Any) -> str:
    """Permit only normal absolute HTTP(S) links inside Telegram HTML."""
    url = str(value or "").strip()
    parsed = urlsplit(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _release_keys(release: dict[str, Any]) -> list[str]:
    """Canonical ID plus platform IDs, kept unique and safe for SQLite keys."""
    keys: list[str] = []
    candidates = [release.get("id"), release.get("dedup_key")]
    candidates.extend(release.get("source_ids", []) or [])
    for candidate in candidates:
        key = str(candidate or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _chat_storage_key(chat_id: int, release_key: str) -> str:
    """Use the unchanged one-column schema while making seen state per chat."""
    return f"chat:{chat_id}:{release_key}"


def _release_seen(chat_id: int, release: dict[str, Any]) -> bool:
    return any(
        store.is_release_seen(_chat_storage_key(chat_id, key))
        for key in _release_keys(release)
    )


def _mark_release_seen(chat_id: int, release: dict[str, Any]) -> None:
    for key in _release_keys(release):
        store.mark_release_seen(_chat_storage_key(chat_id, key))


def _is_recent_release(release: dict[str, Any]) -> bool:
    """Defence in depth for the engine's day-precision lookback filter."""
    try:
        released = date.fromisoformat(str(release.get("release_date", ""))[:10])
    except ValueError:
        return False
    today = date.today()
    lookback = _int_setting("RELEASE_LOOKBACK_DAYS", 5, 1)
    return today - timedelta(days=lookback) <= released <= today


def _release_message(artist_name: str, release: dict[str, Any]) -> str:
    """Format one alert, naming the guest when the tracked artist is featured."""
    release_type = str(release.get("type") or "release").upper()
    title = _escape(release.get("name") or "Untitled")
    tracked = _escape(artist_name)
    # The lead act owns the record; everyone else credited on it is a guest.
    primary = _escape(release.get("artist_name") or artist_name)
    others = [str(name) for name in release.get("credited_artists") or []]
    url = _safe_http_url(release.get("url"))
    link = f'<a href="{_escape(url)}">Listen now</a>' if url else "Link unavailable"

    lines = [f"🚨 <b>NEW {release_type} DROP!</b>", "", f"👤 <b>Artist:</b> {primary}"]
    if release.get("is_feature"):
        # A guest appearance names the act being featured, never a remix of it.
        lines.append(f"🎤 <b>Featuring:</b> {tracked}")
        others = [name for name in others if _escape(name) != tracked]
    if others:
        lines.append(f"🤝 <b>With:</b> {_escape(', '.join(others))}")
    lines += [
        f"💿 <b>Title:</b> {title}",
        f"🎵 <b>Listen:</b> {link}",
        "🎧 <i>Source: Spotify</i>",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    is_admin = _claim_admin(update)
    note = ""
    if is_admin:
        note = "\n\n✅ <b>Alerts will be sent to this account.</b>"
    elif not admin_chat_id():
        note = "\n\n⚠️ Send me a private <code>/start</code> so alerts have a destination."
    else:
        note = "\n\nℹ️ Alerts already go to a different admin account."
    await update.message.reply_text(
        "🎧 <b>Spotify Release Radar Online</b>\n\n"
        "• <code>/add [name or link]</code> — Track an artist on Spotify\n"
        "• <code>/bulkadd [names/links]</code> — Track several artists\n"
        "• Upload a <code>.txt</code>, <code>.csv</code>, or <code>.json</code> file to import\n"
        "• <code>/list</code> — View tracked artists\n"
        "• <code>/remove [name or link]</code> — Stop tracking an artist\n"
        "• <code>/bulkremove [names]</code> — Remove several artists\n"
        "• <code>/status</code> — API health and next scan time\n"
        "• <code>/id</code> — Show where alerts are sent\n"
        "• <code>/pause</code> — Pause the daily scan\n"
        "• <code>/resume</code> — Resume the daily scan"
        f"{note}",
        parse_mode=ParseMode.HTML,
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current notification destination and the caller's own IDs."""
    if not update.message:
        return
    destination = admin_chat_id()
    target = (
        f"your account <code>{destination}</code>"
        if destination and update.effective_user
        and update.effective_user.id == destination
        else (
            f"admin account <code>{destination}</code>"
            if destination
            else "nowhere yet — send a private <code>/start</code>"
        )
    )
    await update.message.reply_text(
        "🆔 <b>Alert destination</b>\n"
        f"┗ {_escape(target)}\n"
        f"┗ Your user ID: <code>{_owner_id(update)}</code>\n"
        f"┗ Your chat ID: <code>{update.effective_chat.id if update.effective_chat else 0}</code>",
        parse_mode=ParseMode.HTML,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report Spotify API health, tracked counts and the next scheduled scan."""
    if not update.message:
        return
    message = await update.message.reply_text(
        "🔍 <i>Running Spotify diagnostics…</i>", parse_mode=ParseMode.HTML
    )
    try:
        report = await run_blocking(music.get_status_report)
        lines = ["📊 <b>Spotify Radar Status</b>", ""]
        for service, info in report.items():
            status = str(info.get("status", "UNKNOWN")).upper()
            details = str(info.get("details", info.get("log", "No details provided")))
            icon = {"ONLINE": "🟢", "WARNING": "🟡"}.get(status, "🔴")
            lines.append(f"{icon} <b>{_escape(service)}</b>: <code>{_escape(status)}</code>")
            lines.append(f"┗ <i>Log:</i> {_escape(details[:400])}")
            lines.append("")

        total_artists = len(store.get_artist_rows())
        destination = admin_chat_id()
        lines.append("📈 <b>Tracked artists</b>")
        lines.append(f"┗ Spotify: {total_artists}")
        lines.append(
            f"┗ Alerts to: {_escape(str(destination)) if destination else 'NOT SET'}"
        )
        lines.append("")

        lines.append("🕐 <b>Scan schedule</b>")
        lines.append("┗ Daily at 05:35 IST (Asia/Kolkata)")
        lines.append(f"┗ Next run: {_next_scan_hint(context)}")
        lines.append("")

        lines.append("🔁 <b>Last scan</b>")
        if SCAN_STATE["last"]:
            lines.append(f"┗ {_escape(SCAN_STATE['last'])}")
            lines.append(
                f"┗ Recent releases {SCAN_STATE['found']}, "
                f"alerts sent {SCAN_STATE['sent']}"
            )
        else:
            lines.append("┗ <i>No scan has completed yet.</i>")
        await message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception:
        LOGGER.exception("/status failed")
        await message.edit_text(
            "❌ Diagnostic check failed. Check the server logs for details."
        )


def _next_scan_hint(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Describe when the daily job next fires, and whether it is paused.

    ``next_run_time`` is absent only while the scheduler is still tentative
    (during start-up) and is ``None`` for a paused job, so both cases are
    reported differently rather than raising inside ``/status``.
    """
    application = getattr(context, "application", None)
    job_queue = getattr(application, "job_queue", None)
    if job_queue is None:
        return "unknown"
    jobs = job_queue.get_jobs_by_name(SCAN_JOB_NAME)
    if not jobs:
        return "not scheduled"
    try:
        next_run = jobs[0].next_run_time
    except AttributeError:
        return "scheduled"
    if next_run is None:
        return "paused"
    return next_run.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _owner_id(update: Update) -> int:
    """Return the id that owns subscriptions created from this update.

    Alerts are routed to a fixed chat rather than to whichever chat a command was
    typed in, so ownership belongs to the person. Storing the user id also keeps
    an artist's row unique no matter which chat the command came from.
    """
    if update.effective_user and update.effective_user.id:
        return update.effective_user.id
    return update.effective_chat.id if update.effective_chat else 0


def _visible_rows(update: Update) -> list[dict[str, Any]]:
    """Return the rows the requester may manage: their own plus the current chat's."""
    scope = {_owner_id(update)}
    if update.effective_chat:
        scope.add(update.effective_chat.id)
    return [row for row in store.get_artist_rows() if row["chat_id"] in scope]


async def list_artists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    rows = _visible_rows(update)
    if not rows:
        await update.message.reply_text("You are not tracking any artists yet.")
        return
    lines = [
        f"• {_escape(row['name'])}"
        for row in sorted(rows, key=lambda r: str(r["name"]).casefold())
    ]
    header = f"📋 <b>Tracked artists ({len(rows)}):</b>\n\n"
    body = "\n".join(lines)
    for start_index in range(0, len(body), 3600):
        chunk = body[start_index : start_index + 3600]
        text = header + chunk if start_index == 0 else chunk
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def _track_artist(target: Any, chat_id: int, query: str) -> None:
    """Resolve an artist on Spotify and store the subscription.

    ``target`` is any object exposing ``reply_text`` (a Message or an edited
    message).
    """
    artist, error = await run_blocking(music.get_artist_info, query)
    if not artist:
        text = f"❌ <b>Spotify</b> could not find <code>{_escape(query)}</code>."
        if error:
            text += f" <i>{_escape(str(error)[:160])}</i>"
        await target.reply_text(text, parse_mode=ParseMode.HTML)
        return

    if store.add_artist(artist["id"], artist["name"], chat_id):
        notes = (
            ""
            if admin_chat_id()
            else "\n<i>Send the bot a private /start so alerts have a destination.</i>"
        )
        await target.reply_text(
            f"✅ Tracking <b>{_escape(artist['name'])}</b> on "
            f"<b>Spotify</b>.{notes}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await target.reply_text(
            f"ℹ️ <b>{_escape(artist['name'])}</b> is already tracked on Spotify.",
            parse_mode=ParseMode.HTML,
        )


async def add_artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not context.args:
        await update.message.reply_text(
            "Please provide an artist link or name. Example: <code>/add Lauv</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _track_artist(update.message, _owner_id(update), " ".join(context.args).strip())


async def bulk_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    raw_text = re.sub(
        r"^/bulkadd(?:@\w+)?\s*", "", update.message.text or "", flags=re.I
    )
    if not raw_text.strip():
        await update.message.reply_text(
            "Provide names or links separated by commas or new lines.\n"
            "Example: <code>/bulkadd Lauv, Ed Sheeran</code>"
        )
        return
    raw_text = re.sub(r"(?<!^)(https://)", r"\n\1", raw_text)
    await process_batch_import(re.split(r"[\r\n,\t]+", raw_text), update)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return
    document = update.message.document
    filename = (document.file_name or "").lower()
    if not filename.endswith((".txt", ".csv", ".json")):
        await update.message.reply_text(
            "❌ Please upload a <code>.txt</code>, <code>.csv</code>, or "
            "<code>.json</code> file."
        )
        return
    if document.file_size and document.file_size > MAX_IMPORT_BYTES:
        await update.message.reply_text(
            f"❌ Files must be no larger than {MAX_IMPORT_BYTES // 1024} KB."
        )
        return

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        contents = (await telegram_file.download_as_bytearray()).decode(
            "utf-8", errors="replace"
        )
    except TelegramError:
        LOGGER.exception("Unable to download import document")
        await update.message.reply_text(
            "❌ Could not download that file. Please try again."
        )
        return

    queries: list[str] = []
    if filename.endswith(".json"):
        try:
            parsed = json.loads(contents)
        except json.JSONDecodeError:
            await update.message.reply_text("❌ The JSON file is not valid.")
            return
        entries = parsed.get("artists", []) if isinstance(parsed, dict) else parsed
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, str):
                    queries.append(entry)
                elif isinstance(entry, dict):
                    value = entry.get("name") or entry.get("uri") or entry.get("url")
                    if isinstance(value, str):
                        queries.append(value)
    else:
        contents = re.sub(r"(?<!^)(https://)", r"\n\1", contents)
        queries = [
            entry.strip() for entry in re.split(r"[\r\n,]+", contents) if entry.strip()
        ]

    if not queries:
        await update.message.reply_text(
            "❌ No valid artist entries were found in that file."
        )
        return
    await process_batch_import(queries, update)


async def process_batch_import(queries: list[str], update: Update) -> None:
    """Resolve, save each artist independently on Spotify."""
    if not update.message or not update.effective_chat:
        return
    chat_id = _owner_id(update)
    cleaned = list(
        dict.fromkeys(query.strip() for query in queries if query and query.strip())
    )
    if len(cleaned) > MAX_IMPORT_ARTISTS:
        await update.message.reply_text(
            f"❌ Import is limited to {MAX_IMPORT_ARTISTS} artists at a time."
        )
        return
    status_message = await update.message.reply_text(
        f"⏳ Processing {len(cleaned)} artist(s) on Spotify…"
    )
    added: list[str] = []
    already: list[str] = []
    failed: list[str] = []

    for query in cleaned:
        try:
            artist, error = await run_blocking(music.get_artist_info, query)
            if not artist:
                failed.append(f"{query} — {error}" if error else query)
                continue
            if store.add_artist(artist["id"], artist["name"], chat_id):
                added.append(artist["name"])
            else:
                already.append(artist["name"])
        except Exception:
            LOGGER.exception("Import failed for query %r", query)
            failed.append(query)
        await asyncio.sleep(_float_setting("IMPORT_DELAY_SECONDS", 0.5))

    lines = [
        "✅ <b>Import complete</b> (Spotify)",
        "",
        f"• <b>Added ({len(added)}):</b> {_escape(', '.join(added) or 'None')}",
    ]
    if already:
        lines.append(
            f"• <b>Already tracked ({len(already)}):</b> {_escape(', '.join(already))}"
        )
    if failed:
        lines.append(
            f"• <b>Failed / not found ({len(failed)}):</b> "
            f"{_escape(', '.join(failed))}"
        )
    summary = "\n".join(lines)
    if len(summary) > 4000:
        summary = summary[:3900] + "\n… truncated"
    await status_message.edit_text(summary, parse_mode=ParseMode.HTML)


async def remove_artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/remove &lt;artist name or link&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    raw_query = " ".join(context.args).strip()
    removed: list[str] = []
    for row in _visible_rows(update):
        name = str(row["name"] or "")
        if raw_query.casefold() in name.casefold():
            store.remove_artist(row["artist_key"], row["chat_id"])
            removed.append(name)
    if removed:
        await update.message.reply_text(
            f"🗑️ Removed: <b>{_escape(', '.join(removed))}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ No matching artist found for <code>{_escape(raw_query)}</code>.",
            parse_mode=ParseMode.HTML,
        )


async def bulk_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    raw_text = re.sub(
        r"^/bulkremove(?:@\w+)?\s*", "", update.message.text or "", flags=re.I
    )
    if not raw_text.strip():
        await update.message.reply_text(
            "Usage: <code>/bulkremove Name 1, Name 2</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    targets = [
        target.strip().casefold()
        for target in re.split(r"[,\n]+", raw_text)
        if target.strip()
    ]
    removed: list[str] = []
    for row in _visible_rows(update):
        name = str(row["name"] or "")
        if any(target in name.casefold() for target in targets):
            store.remove_artist(row["artist_key"], row["chat_id"])
            removed.append(name)
    if removed:
        await update.message.reply_text(
            "🗑️ Bulk removed ({0}):\n• ".format(len(removed))
            + "\n• ".join(_escape(name) for name in removed),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "❌ None of those artists were found in your tracking list."
        )


# ----------------------------------------------------------------------
# Scheduled scanning
# ----------------------------------------------------------------------
async def _deliver(
    context: ContextTypes.DEFAULT_TYPE,
    entries: list[tuple[str, dict[str, Any]]],
    fallback_chat_id: int,
) -> int:
    """Send every release that has not been announced to the destination yet."""
    if not entries:
        return 0
    destination = admin_chat_id() or fallback_chat_id
    if not destination:
        LOGGER.warning(
            "No alert destination is set; %d release(s) dropped. Send a private "
            "/start to the bot.",
            len(entries),
        )
        return 0

    sent = 0
    for artist_name, release in entries:
        if not _is_recent_release(release) or _release_seen(destination, release):
            continue
        try:
            await context.bot.send_message(
                chat_id=destination,
                text=_release_message(artist_name, release),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        except TelegramError:
            LOGGER.exception(
                "Telegram delivery failed for %s to chat %s",
                artist_name,
                destination,
            )
            continue
        _mark_release_seen(destination, release)
        sent += 1
    return sent


async def _scan(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, int]:
    """Check every tracked artist once. Returns ``(checked, sent)``."""
    rows = store.get_artist_rows()
    if not rows:
        LOGGER.info("No artists tracked; nothing to scan.")
        return 0, 0

    entries: list[tuple[str, dict[str, Any]]] = []
    checked = 0
    fallback_chat_id = 0
    for row in rows:
        artist_id = str(row.get("spotify_id") or "")
        if not artist_id:
            continue
        fallback_chat_id = fallback_chat_id or int(row["chat_id"] or 0)
        try:
            releases = await run_blocking(
                music.check_spotify_releases, artist_id, row["name"]
            )
        except Exception:
            LOGGER.exception("Spotify scan failed for %s", row["name"])
            continue
        if releases:
            entries.extend((row["name"], release) for release in releases)
        checked += 1
        if CHECK_DELAY_SECONDS:
            await asyncio.sleep(CHECK_DELAY_SECONDS)

    sent = await _deliver(context, entries, fallback_chat_id)

    SCAN_STATE["last"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SCAN_STATE["found"] = int(SCAN_STATE.get("found", 0)) + len(entries)
    SCAN_STATE["sent"] = int(SCAN_STATE.get("sent", 0)) + sent
    LOGGER.info(
        "Spotify scan: %d artist(s) checked, %d release(s) found, %d alert(s) sent",
        checked,
        len(entries),
        sent,
    )
    return checked, sent


async def daily_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run one full Spotify pass, scheduled daily at 05:35 IST."""
    await _scan(context)


async def pause_scanning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause the daily Spotify scan job."""
    if not update.message:
        return
    job_queue = context.application.job_queue
    if not job_queue:
        await update.message.reply_text("❌ Job queue unavailable.")
        return
    jobs = job_queue.get_jobs_by_name(SCAN_JOB_NAME)
    if not jobs:
        await update.message.reply_text("❌ Scan job not found.")
        return
    for job in jobs:
        job.pause()
    await update.message.reply_text(
        "⏸️ <b>Spotify scanning paused.</b> /resume restarts it.",
        parse_mode=ParseMode.HTML,
    )


async def resume_scanning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume the daily Spotify scan job."""
    if not update.message:
        return
    job_queue = context.application.job_queue
    if not job_queue:
        await update.message.reply_text("❌ Job queue unavailable.")
        return
    jobs = job_queue.get_jobs_by_name(SCAN_JOB_NAME)
    if not jobs:
        await update.message.reply_text("❌ Scan job not found.")
        return
    for job in jobs:
        job.resume()
    await update.message.reply_text(
        "▶️ <b>Spotify scanning resumed.</b>", parse_mode=ParseMode.HTML
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled Telegram update error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("CRITICAL ERROR: BOT_TOKEN is not configured.")
    if not admin_chat_id():
        LOGGER.warning(
            "No alert destination yet. Send the bot a private /start and it will "
            "remember that account."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("add", add_artist))
    app.add_handler(CommandHandler("bulkadd", bulk_add))
    app.add_handler(CommandHandler("list", list_artists))
    app.add_handler(CommandHandler("remove", remove_artist))
    app.add_handler(CommandHandler("bulkremove", bulk_remove))
    app.add_handler(CommandHandler("pause", pause_scanning))
    app.add_handler(CommandHandler("resume", resume_scanning))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)

    if app.job_queue is None:
        raise SystemExit(
            "Job queue is unavailable; install python-telegram-bot[job-queue]."
        )

    # Once a day at 05:35 IST. The time is timezone-aware, so this is 05:35 in
    # India regardless of where the server is hosted.
    app.job_queue.run_daily(
        daily_scan_job,
        time=SCAN_TIME,
        name=SCAN_JOB_NAME,
        # Never stack scans: a slow run must be skipped, not queued behind.
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    LOGGER.info(
        "Spotify Release Radar online | %d artist(s) tracked | daily scan at 05:35 IST",
        len(store.get_artist_rows()),
    )
    app.run_polling()


if __name__ == "__main__":
    main()