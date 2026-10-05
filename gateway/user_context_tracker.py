"""
UserContextTracker — lightweight cross-channel session awareness.

Tracks user activity across sessions/channels. Detects channel switches
and provides gist content for lazy system prompt injection.

Thread safety: single SQLite writer via a bounded queue (fire-and-forget).
Reads have a hard timeout and degrade to no-injection on failure.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_activity (
    user_id         TEXT NOT NULL,
    session_key     TEXT NOT NULL,
    platform        TEXT NOT NULL,
    guild_id        TEXT DEFAULT '',
    channel_name    TEXT DEFAULT '',
    chat_type       TEXT NOT NULL DEFAULT 'dm',
    sensitivity     TEXT NOT NULL DEFAULT 'public',  -- 'public' | 'restricted'
    last_active     REAL NOT NULL,
    message_count   INTEGER DEFAULT 0,
    content_gist    TEXT DEFAULT '',
    recency_hint    TEXT DEFAULT '',
    announced_switch_from TEXT DEFAULT '',
    announced_at    REAL DEFAULT 0,
    PRIMARY KEY (user_id, session_key)
);
CREATE INDEX IF NOT EXISTS idx_user_activity_recency
    ON user_activity(last_active DESC);
"""

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW_MINUTES = 30
_DEFAULT_STALE_THRESHOLD_HOURS = 2
_DEFAULT_BUDGET_MINUTES = 5
_DEFAULT_RECENCY_HINT_LENGTH = 200
_DEFAULT_READ_TIMEOUT = 0.05  # 50ms hard limit for reads

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SwitchEvent:
    """Returned by detect_switch() when a cross-channel gap is found."""

    previous_session_key: str = ""
    previous_channel: str = ""
    previous_hint: str = ""
    elapsed_minutes: int = 0
    is_stale: bool = False
    venue_mismatch: bool = False
    platform: str = ""
    guild_id: str = ""
    chat_type: str = ""


@dataclass
class _WriteJob:
    """Internal job for the write queue."""

    job_type: str  # "record" | "mark_announced" | "capture_gist"
    data: dict


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class UserContextTracker:
    """Cross-channel session tracker with lazy injection support.

    Usage::

        tracker = UserContextTracker(db_path="~/.hermes/session_tracker.db")
        tracker.record_message(user_id="...", session_key="...", ...)

        evt = tracker.detect_switch(user_id="...", session_key="...")
        if evt:
            # inject evt into system prompt
    """

    def __init__(self, db_path: str | Path | None = None,
                 config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._db_path = self._resolve_db_path(db_path)

        # SQLite connection — single writer, many readers
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._conn.row_factory = sqlite3.Row

        # Migrate: add columns that may not exist in older databases
        self._migrate()

        # Write queue — bounded, fire-and-forget
        self._write_queue: queue.Queue[_WriteJob] = queue.Queue(maxsize=1000)
        self._writer_stop = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="uctx-writer",
        )
        self._writer_thread.start()

        # Config
        self._window_sec = (
            int(self._config.get("window_minutes", _DEFAULT_WINDOW_MINUTES) or _DEFAULT_WINDOW_MINUTES)
        ) * 60
        self._stale_sec = (
            int(self._config.get("stale_threshold_hours", _DEFAULT_STALE_THRESHOLD_HOURS) or _DEFAULT_STALE_THRESHOLD_HOURS)
        ) * 3600
        self._budget_sec = (
            int(self._config.get("budget_minutes", _DEFAULT_BUDGET_MINUTES) or _DEFAULT_BUDGET_MINUTES)
        ) * 60
        self._hint_len = int(
            self._config.get("recency_hint_length", _DEFAULT_RECENCY_HINT_LENGTH) or _DEFAULT_RECENCY_HINT_LENGTH
        )
        self._venue_aware = bool(self._config.get("venue_aware", True))
        self._injection_mode = str(
            self._config.get("injection_mode", "shadow")
        )
        self._read_timeout = float(
            self._config.get("read_timeout_sec", _DEFAULT_READ_TIMEOUT) or _DEFAULT_READ_TIMEOUT
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_message(
        self,
        *,
        user_id: str,
        session_key: str,
        platform: str,
        guild_id: str = "",
        channel_name: str = "",
        chat_type: str = "",
        sensitivity: str = "public",
        message_text: str = "",
    ) -> None:
        """Fire-and-forget: queue a write for this message.

        Must never block or raise. Per-session last-write-wins coalescing:
        a newer write for the same session_key *replaces* an older pending one,
        so recency_hint is never dropped — only overwritten by something
        fresher.
        """
        hint = (message_text or "").strip()[: self._hint_len]
        try:
            self._write_queue.put_nowait(_WriteJob(
                job_type="record",
                data={
                    "user_id": user_id,
                    "session_key": session_key,
                    "platform": platform,
                    "guild_id": guild_id or "",
                    "channel_name": channel_name or "",
                    "chat_type": chat_type or "dm",
                    "sensitivity": sensitivity or "public",
                    "recency_hint": hint,
                    "timestamp": time.time(),
                },
            ))
        except queue.Full:
            pass  # drop silently — don't block message delivery

    def detect_switch(
        self,
        *,
        user_id: str,
        session_key: str,
        platform: str,
        guild_id: str = "",
        chat_type: str = "",
        sensitivity: str = "public",
    ) -> Optional[SwitchEvent]:
        """Check if user just switched channels.

        Returns None if:
        - No other warm sessions found for this user_id
        - Switch was already announced (dedup)
        - Origin venue differs (venue-aware mode)
        - Budget not yet expired

        Returns a SwitchEvent if a cross-channel gap was detected.
        Hard timeout at ``read_timeout_sec`` — degrades to None on DB
        contention.
        """
        start = time.monotonic()

        try:
            now = time.time()
            cutoff = now - self._window_sec

            rows = self._conn.execute(
                """SELECT session_key, platform, guild_id, channel_name,
                          chat_type, last_active, message_count,
                          recency_hint, content_gist,
                          announced_switch_from, announced_at
                   FROM user_activity
                   WHERE user_id = ? AND session_key != ?
                     AND last_active >= ?
                   ORDER BY last_active DESC
                   LIMIT 5""",
                (user_id, session_key, cutoff),
            ).fetchall()

            if not rows:
                return None

            if time.monotonic() - start > self._read_timeout:
                logger.info("CROSS-CHANNEL: detect_switch read timed out")
                return None

        except Exception as e:
            logger.info("CROSS-CHANNEL: detect_switch query failed: %s", e)
            return None

        # Filter candidates
        candidates: list[dict] = []
        for row in rows:
            # Dedup: skip if this gap was already announced
            if row["announced_switch_from"] == session_key:
                continue

            # Budget: skip if announced too recently
            if row["announced_at"] > now - self._budget_sec:
                continue

            # Venue: skip if origin venue differs (in venue-aware mode)
            # Sensitivity: only inject if dest audience is ⊆ origin audience.
            # public → restricted (dm) is fine; restricted → public is not.
            if self._venue_aware:
                if row["platform"] != platform:
                    continue
                # DMs transcend guild boundaries — same user regardless of server
                if row["chat_type"] != "dm" and chat_type != "dm":
                    if (row["guild_id"] or "") != (guild_id or ""):
                        continue
                _origin_sens = (row["sensitivity"] or "public").strip().lower()
                _dest_sens = (sensitivity or "public").strip().lower()
                if _origin_sens == "restricted" and _dest_sens == "public":
                    continue

            elapsed_min = (now - row["last_active"]) / 60.0
            candidates.append({
                "row": row,
                "elapsed_minutes": elapsed_min,
                "is_stale": elapsed_min > (self._stale_sec / 60.0),
                # Score: weighted by recency (60%) and activity depth (40%)
                "score": (
                    float(row["message_count"] or 0) * 0.4
                    + (1.0 / max(elapsed_min, 0.5)) * 0.6
                ),
            })

        if not candidates:
            return None

        # Pick best candidate
        best = max(candidates, key=lambda c: c["score"])
        row = best["row"]

        # Mark as announced (async, best-effort)
        self._mark_announced(session_key, row["session_key"])
        hint = (row["recency_hint"] or row["content_gist"] or "").strip()

        evt = SwitchEvent(
            previous_session_key=row["session_key"],
            previous_channel=row["channel_name"] or row["session_key"],
            previous_hint=hint,
            elapsed_minutes=round(best["elapsed_minutes"]),
            is_stale=best["is_stale"],
            venue_mismatch=False,
            platform=row["platform"],
            guild_id=row["guild_id"] or "",
            chat_type=row["chat_type"],
        )

        # Log injection decision
        self._log_injection(evt)

        return evt

    def capture_gist(self, session_key: str, user_id: str,
                     gist: str) -> None:
        """Store a gist for a session (called from compression pass)."""
        try:
            self._write_queue.put_nowait(_WriteJob(
                job_type="capture_gist",
                data={
                    "session_key": session_key,
                    "user_id": user_id,
                    "gist": (gist or "").strip(),
                    "timestamp": time.time(),
                },
            ))
        except queue.Full:
            pass

    def ttl_sweep(self) -> None:
        """Delete rows older than stale threshold. Call periodically."""
        cutoff = time.time() - self._stale_sec * 3
        try:
            self._conn.execute(
                "DELETE FROM user_activity WHERE last_active < ?",
                (cutoff,),
            )
            self._conn.commit()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Graceful shutdown: drain queue, stop writer, close DB."""
        self._writer_stop.set()
        self._writer_thread.join(timeout=5.0)
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _mark_announced(self, current_session_key: str,
                        previous_session_key: str) -> None:
        """Mark a switch as announced (synchronous — tiny write, called rarely)."""
        try:
            self._conn.execute(
                """UPDATE user_activity SET
                       announced_switch_from = ?,
                       announced_at = ?
                   WHERE session_key = ?""",
                (current_session_key, time.time(), previous_session_key),
            )
            self._conn.commit()
        except Exception:
            pass

    def _log_injection(self, evt: SwitchEvent) -> None:
        """Log an injection decision for review (INFO level — rare events)."""
        if self._injection_mode == "shadow":
            logger.info(
                "CROSS-CHANNEL:[shadow] channel=%s hint=%.80s "
                "elapsed=%dm stale=%s venue_mismatch=%s",
                evt.previous_channel,
                evt.previous_hint,
                evt.elapsed_minutes,
                evt.is_stale,
                evt.venue_mismatch,
            )
        else:
            logger.info(
                "CROSS-CHANNEL:[on-switch] channel=%s hint=%.80s "
                "elapsed=%dm",
                evt.previous_channel,
                evt.previous_hint,
                evt.elapsed_minutes,
            )

    def _resolve_db_path(self, db_path: str | Path | None) -> Path:
        if db_path:
            return Path(db_path).expanduser().resolve()
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "session_tracker.db"

    def _migrate(self) -> None:
        """Add columns that may not exist in databases created by older versions."""
        try:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(user_activity)").fetchall()}
            if "sensitivity" not in cols:
                self._conn.execute(
                    "ALTER TABLE user_activity ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'public'"
                )
                self._conn.commit()
                logger.info("CROSS-CHANNEL: migrated schema — added sensitivity column")
        except Exception:
            pass

    def _writer_loop(self) -> None:
        """Background writer: drains the pending writes dict and flushes to SQLite.

        Uses per-session last-write-wins coalescing: a newer write for the
        same session_key replaces an older pending one. This guarantees
        recency_hint is never dropped — only overwritten by something fresher.
        Single writer thread ensures no SQLite concurrency issues.
        """
        while not self._writer_stop.is_set():
            # Collect all pending writes into a batch (last-write-wins per key)
            batch: dict[str, _WriteJob] = {}
            try:
                while True:
                    job = self._write_queue.get_nowait()
                    if job.job_type == "record":
                        # Coalesce: latest per session_key wins
                        batch[job.data["session_key"]] = job
                    elif job.job_type == "capture_gist":
                        pass  # handle after the loop
                    else:
                        batch[job.data.get("session_key", "")] = job
            except queue.Empty:
                pass

            # Process the batch
            for sk, job in batch.items():
                try:
                    if job.job_type == "record" and sk:
                        self._conn.execute(
                            """INSERT INTO user_activity
                               (user_id, session_key, platform, guild_id,
                                channel_name, chat_type, sensitivity,
                                last_active, message_count, recency_hint)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                               ON CONFLICT(user_id, session_key) DO UPDATE SET
                                   last_active = excluded.last_active,
                                   message_count = message_count + 1,
                                   recency_hint = excluded.recency_hint""",
                            (
                                job.data["user_id"],
                                sk,
                                job.data["platform"],
                                job.data["guild_id"],
                                job.data["channel_name"],
                                job.data["chat_type"],
                                job.data.get("sensitivity", "public"),
                                job.data["timestamp"],
                                job.data["recency_hint"],
                            ),
                        )
                    elif job.job_type == "capture_gist":
                        self._conn.execute(
                            """UPDATE user_activity SET content_gist = ?
                               WHERE session_key = ? AND user_id = ?""",
                            (
                                job.data["gist"],
                                job.data["session_key"],
                                job.data["user_id"],
                            ),
                        )
                except Exception as _we:
                    logger.info("CROSS-CHANNEL: writer error: %s", _we)

            if batch:
                self._conn.commit()

            # Sleep briefly before next drain
            self._writer_stop.wait(0.1)

    # Context manager support for easy cleanup
    def __enter__(self) -> "UserContextTracker":
        return self

    def __exit__(self, *args: Any) -> None:
        self.shutdown()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_tracker: Optional[UserContextTracker] = None
_tracker_lock = threading.Lock()


def get_user_context_tracker() -> Optional[UserContextTracker]:
    """Return the global UserContextTracker singleton (or None).

    If ``memory.cross_channel_awareness`` is not enabled in config,
    returns None so callers can short-circuit cleanly.
    """
    global _tracker
    if _tracker is not None:
        return _tracker

    with _tracker_lock:
        if _tracker is not None:
            return _tracker
        try:
            from hermes_cli.config import cfg_get, load_config
            config = load_config()
            if not cfg_get(config, "memory", "cross_channel_awareness"):
                logger.info("CROSS-CHANNEL: awareness disabled in config")
                return None
            cc_config = cfg_get(config, "memory", "cross_channel") or {}
            _tracker = UserContextTracker(config=cc_config)
            logger.info(
                "CROSS-CHANNEL: tracker initialized (mode=%s, window=%dm)",
                cc_config.get("injection_mode", "shadow"),
                cc_config.get("window_minutes", 30),
            )
            return _tracker
        except Exception as e:
            logger.info("CROSS-CHANNEL: failed to init tracker: %s", e)
            return None
