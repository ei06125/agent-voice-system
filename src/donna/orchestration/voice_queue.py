"""Durable shadow queue for finalized Donna voice commands."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MESSAGE_STATES = ("pending", "leased", "accepted", "completed", "dead_letter")
DEFAULT_STATE_DIR = Path(
    os.environ.get("DONNA_VOICE_STATE_DIR", "~/.claude/donna")
).expanduser()
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "DONNA_VOICE_QUEUE_DB", str(DEFAULT_STATE_DIR / "voice-delivery.sqlite3")
    )
).expanduser()
DEFAULT_SOCKET_PATH = Path(
    os.environ.get("DONNA_VOICE_QUEUE_SOCKET", str(DEFAULT_STATE_DIR / "delivery.sock"))
).expanduser()


class DuplicateEventError(ValueError):
    """Raised when a producer identity is reused with different content."""


@dataclass(frozen=True)
class EnqueueResult:
    message_id: str
    inserted: bool


class VoiceQueue:
    def __init__(
        self,
        db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
        socket_path: str | os.PathLike[str] = DEFAULT_SOCKET_PATH,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.socket_path = Path(socket_path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            isolation_level=None,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        parent_existed = self.db_path.parent.exists()
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            os.chmod(self.db_path.parent, 0o700)

        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"voice queue schema {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE messages (
                        message_id TEXT PRIMARY KEY,
                        capture_id TEXT NOT NULL,
                        utterance_seq INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        vad_ended_at REAL,
                        finalized_at REAL NOT NULL,
                        text TEXT NOT NULL CHECK (length(trim(text)) > 0),
                        state TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN (
                                'pending', 'leased', 'accepted',
                                'completed', 'dead_letter'
                            )),
                        is_shadow INTEGER NOT NULL DEFAULT 1
                            CHECK (is_shadow IN (0, 1)),
                        lease_owner TEXT,
                        lease_until REAL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        target_session TEXT,
                        accepted_at REAL,
                        completed_at REAL,
                        UNIQUE (capture_id, utterance_seq)
                    );
                    CREATE INDEX messages_delivery_order
                        ON messages (is_shadow, state, created_at, message_id);
                    PRAGMA user_version=1;
                    COMMIT;
                    """
                )
                version = 1
            if version == 1:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE messages ADD COLUMN next_attempt_at REAL;
                    ALTER TABLE messages ADD COLUMN leased_at REAL;
                    ALTER TABLE messages ADD COLUMN acceptance_latency_ms REAL;
                    CREATE TABLE accepted_ids (
                        message_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        accepted_at REAL NOT NULL,
                        completed_at REAL,
                        FOREIGN KEY (message_id) REFERENCES messages(message_id)
                    );
                    CREATE TABLE events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT,
                        event TEXT NOT NULL,
                        at REAL NOT NULL,
                        detail TEXT
                    );
                    CREATE INDEX messages_eligible
                        ON messages (is_shadow, target_session, state,
                                     next_attempt_at, created_at);
                    CREATE INDEX events_message ON events(message_id, at);
                    PRAGMA user_version=2;
                    COMMIT;
                    """
                )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")

        os.chmod(self.db_path, 0o600)

    def enqueue(
        self,
        *,
        capture_id: str,
        utterance_seq: int,
        text: str,
        vad_ended_at: float | None = None,
        message_id: str | None = None,
        finalized_at: float | None = None,
        is_shadow: bool = True,
        target_session: str | None = None,
    ) -> EnqueueResult:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("voice command text cannot be empty")
        if not capture_id:
            raise ValueError("capture_id cannot be empty")
        if utterance_seq < 1:
            raise ValueError("utterance_seq must be at least 1")

        message_id = message_id or str(uuid.uuid4())
        if finalized_at is None:
            finalized_at = time.time()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO messages (
                        message_id, capture_id, utterance_seq, created_at,
                        vad_ended_at, finalized_at, text, is_shadow,
                        target_session
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        capture_id,
                        utterance_seq,
                        finalized_at,
                        vad_ended_at,
                        finalized_at,
                        normalized_text,
                        int(is_shadow),
                        target_session,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT message_id, capture_id, utterance_seq, text,
                           is_shadow, target_session
                    FROM messages
                    WHERE message_id = ?
                       OR (capture_id = ? AND utterance_seq = ?)
                    """,
                    (message_id, capture_id, utterance_seq),
                ).fetchone()
                if existing is None:
                    connection.execute("ROLLBACK")
                    raise
                same_event = (
                    existing["capture_id"] == capture_id
                    and existing["utterance_seq"] == utterance_seq
                    and existing["text"] == normalized_text
                    and existing["is_shadow"] == int(is_shadow)
                    and existing["target_session"] == target_session
                )
                if not same_event:
                    connection.execute("ROLLBACK")
                    raise DuplicateEventError(
                        "producer identity already exists with different content"
                    )
                connection.execute("COMMIT")
                return EnqueueResult(existing["message_id"], False)
            connection.execute("COMMIT")

        return EnqueueResult(message_id, True)

    def notify(self) -> bool:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.setblocking(False)
            client.sendto(b"1", str(self.socket_path))
        except OSError:
            return False
        finally:
            client.close()
        return True

    def enqueue_and_notify(self, **kwargs: Any) -> EnqueueResult:
        result = self.enqueue(**kwargs)
        if result.inserted:
            self.notify()
        return result

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT is_shadow, state, count(*) AS count,
                       min(created_at) AS oldest_created_at
                FROM messages
                GROUP BY is_shadow, state
                ORDER BY is_shadow DESC, state
                """
            ).fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        return {
            "database": str(self.db_path),
            "socket": str(self.socket_path),
            "schema_version": version,
            "queues": [dict(row) for row in rows],
        }

    def health(self) -> dict[str, Any]:
        status_path = self.db_path.parent / "delivery-status.json"
        worker: dict[str, Any] | None = None
        if status_path.exists():
            try:
                worker = json.loads(status_path.read_text())
                worker["heartbeat_age_seconds"] = max(
                    0.0, time.time() - float(worker["updated_at"])
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                worker = {"state": "invalid_status_file"}
        result = self.status()
        result.update(
            {
                "socket_present": self.socket_path.exists(),
                "worker": worker,
                "healthy": bool(
                    worker
                    and worker.get("state") in {"ready", "delivering"}
                    and worker.get("heartbeat_age_seconds", 999) < 2
                    and self.socket_path.exists()
                ),
            }
        )
        return result

    def _event(
        self,
        connection: sqlite3.Connection,
        message_id: str | None,
        event: str,
        detail: str | None = None,
        at: float | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(message_id, event, at, detail) VALUES (?, ?, ?, ?)",
            (message_id, event, at if at is not None else time.time(), detail),
        )

    def lease_next(
        self,
        *,
        owner: str,
        target_session: str,
        lease_seconds: float = 15.0,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically recover expired work and lease one non-shadow row."""
        if not owner or not target_session:
            raise ValueError("owner and target_session are required")
        now = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE state='leased' AND lease_until <= ?
                """,
                (now,),
            ).fetchall()
            connection.execute(
                """
                UPDATE messages SET state='pending', lease_owner=NULL,
                    lease_until=NULL, leased_at=NULL
                WHERE state='leased' AND lease_until <= ?
                """,
                (now,),
            )
            for row in expired:
                self._event(connection, row["message_id"], "lease_expired", at=now)
            row = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE is_shadow=0 AND state='pending'
                  AND target_session = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at, message_id LIMIT 1
                """,
                (target_session, now),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            leased = connection.execute(
                """
                UPDATE messages
                SET state='leased', lease_owner=?, lease_until=?, leased_at=?,
                    attempt_count=attempt_count+1, last_error=NULL
                WHERE message_id=? AND state='pending' AND is_shadow=0
                RETURNING *
                """,
                (owner, now + lease_seconds, now, row["message_id"]),
            ).fetchone()
            self._event(connection, row["message_id"], "leased", owner, now)
            connection.execute("COMMIT")
            return dict(leased) if leased else None

    def renew_lease(
        self, message_id: str, owner: str, lease_seconds: float = 15.0
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE messages SET lease_until=?
                WHERE message_id=? AND state='leased' AND lease_owner=?
                """,
                (now + lease_seconds, message_id, owner),
            )
        return result.rowcount == 1

    def is_accepted(self, message_id: str, session_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM accepted_ids WHERE message_id=? AND session_id=?",
                    (message_id, session_id),
                ).fetchone()
                is not None
            )

    def acknowledge(self, message_id: str, owner: str, session_id: str) -> bool:
        """Durably record receiver acceptance and queue state together."""
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT finalized_at FROM messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                """
                INSERT INTO accepted_ids(message_id, session_id, accepted_at)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (message_id, session_id, now),
            )
            result = connection.execute(
                """
                UPDATE messages SET state='accepted', accepted_at=?,
                    acceptance_latency_ms=(?-finalized_at)*1000,
                    lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL
                WHERE message_id=? AND is_shadow=0
                  AND (state='accepted' OR (state='leased' AND lease_owner=?))
                """,
                (now, now, message_id, owner),
            )
            if result.rowcount:
                self._event(connection, message_id, "accepted", session_id, now)
            connection.execute("COMMIT")
            return result.rowcount == 1

    def complete(self, message_id: str, session_id: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE messages SET state='completed', completed_at=?
                WHERE message_id=? AND state='accepted' AND EXISTS (
                    SELECT 1 FROM accepted_ids
                    WHERE message_id=? AND session_id=?
                )
                """,
                (now, message_id, message_id, session_id),
            )
            if result.rowcount:
                connection.execute(
                    "UPDATE accepted_ids SET completed_at=? WHERE message_id=?",
                    (now, message_id),
                )
                self._event(connection, message_id, "completed", session_id, now)
            connection.execute("COMMIT")
            return result.rowcount == 1

    def fail(
        self,
        message_id: str,
        owner: str,
        error: str,
        *,
        max_attempts: int = 5,
        base_backoff: float = 0.5,
    ) -> str:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT attempt_count FROM messages
                   WHERE message_id=? AND state='leased' AND lease_owner=?""",
                (message_id, owner),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RuntimeError("lease is no longer owned by this worker")
            dead = row["attempt_count"] >= max_attempts
            new_state = "dead_letter" if dead else "pending"
            delay = min(60.0, base_backoff * (2 ** max(0, row["attempt_count"] - 1)))
            connection.execute(
                """
                UPDATE messages SET state=?, lease_owner=NULL, lease_until=NULL,
                    leased_at=NULL, last_error=?, next_attempt_at=?
                WHERE message_id=?
                """,
                (new_state, error[:2000], None if dead else now + delay, message_id),
            )
            self._event(connection, message_id, new_state, error[:2000], now)
            connection.execute("COMMIT")
            return new_state

    def replay_dead_letter(self, message_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE messages SET state='pending', attempt_count=0,
                    last_error=NULL, next_attempt_at=NULL
                WHERE message_id=? AND state='dead_letter' AND is_shadow=0
                """,
                (message_id,),
            )
            if result.rowcount:
                self._event(connection, message_id, "replayed")
            connection.execute("COMMIT")
            return result.rowcount == 1

    def prune_completed(self, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ids = connection.execute(
                "SELECT message_id FROM messages WHERE state='completed' AND completed_at < ?",
                (cutoff,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM events WHERE message_id=?",
                [(r["message_id"],) for r in ids],
            )
            connection.executemany(
                "DELETE FROM accepted_ids WHERE message_id=?",
                [(r["message_id"],) for r in ids],
            )
            connection.executemany(
                "DELETE FROM messages WHERE message_id=?",
                [(r["message_id"],) for r in ids],
            )
            connection.execute("COMMIT")
            return len(ids)

    def list_messages(
        self, *, limit: int = 20, include_text: bool = False
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        columns = (
            "message_id, capture_id, utterance_seq, created_at, vad_ended_at, "
            "finalized_at, state, is_shadow, attempt_count, target_session, "
            "lease_owner, lease_until, next_attempt_at, last_error, accepted_at, "
            "completed_at, acceptance_latency_ms"
        )
        if include_text:
            columns += ", text"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM messages "
                "ORDER BY created_at, message_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Donna's voice queue")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("status")
    subparsers.add_parser("health")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--include-text", action="store_true")
    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("message_id")
    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--completed-older-than-days", type=float, default=30)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    voice_queue = VoiceQueue(args.db, args.socket)
    if args.command == "init":
        result: Any = voice_queue.status()
    elif args.command == "status":
        result = voice_queue.status()
    elif args.command == "health":
        result = voice_queue.health()
    elif args.command == "list":
        result = voice_queue.list_messages(
            limit=args.limit, include_text=args.include_text
        )
    elif args.command == "replay":
        result = {"replayed": voice_queue.replay_dead_letter(args.message_id)}
    else:
        result = {
            "pruned": voice_queue.prune_completed(
                args.completed_older_than_days * 86400
            )
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
