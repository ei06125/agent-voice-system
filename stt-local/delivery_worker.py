"""Low-latency, crash-safe delivery worker for Donna voice commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from playback_control import cancel_playback
from voice_queue import DEFAULT_DB_PATH, DEFAULT_SOCKET_PATH, VoiceQueue

# One file per message, not a single shared path: a shared path would let
# a second completion's write silently overwrite (os.replace) a first
# reply the watcher hasn't consumed yet, losing it outright. Same class of
# bug as the kokoro-local playback-flag clobber fixed earlier tonight -
# per-ID file naming avoids it structurally instead of relying on timing.
REPLY_READY_DIR = DEFAULT_SOCKET_PATH.parent


class DeliverySink(Protocol):
    session_id: str

    async def accepted_ids(self) -> set[str]: ...
    async def submit(self, message_id: str, text: str) -> None: ...
    async def wait_completed(self, message_id: str) -> None: ...
    async def close(self) -> None: ...


class DeliveryWorker:
    def __init__(
        self,
        queue: VoiceQueue,
        sink: DeliverySink,
        *,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        scan_interval: float = 0.1,
        lease_seconds: float = 15,
        max_attempts: int = 5,
        status_path: Path | None = None,
        reply_ready_dir: Path | None = None,
    ) -> None:
        self.queue = queue
        self.sink = sink
        self.socket_path = Path(socket_path)
        self.scan_interval = scan_interval
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.status_path = status_path or queue.db_path.parent / "delivery-status.json"
        self.reply_ready_dir = reply_ready_dir or REPLY_READY_DIR
        self.owner = f"{os.getpid()}-{uuid.uuid4()}"
        self._stopping = False
        self._completion_tasks: set[asyncio.Task[None]] = set()

    def _status(self, **extra: object) -> None:
        data = {
            "pid": os.getpid(),
            "worker_id": self.owner,
            "session_id": self.sink.session_id,
            "updated_at": time.time(),
            **extra,
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.status_path)

    def _write_reply_ready(self, message_id: str, prompt: str, reply: str) -> None:
        """Signal file for an external watcher (e.g. an fswatch/Monitor
        loop) - written the moment a reply is confirmed complete. The
        reader deletes it after speaking; existence alone is the signal,
        content is the payload. One file per message_id so a fast second
        completion can never clobber a first reply the watcher hasn't
        consumed yet."""
        data = {
            "message_id": message_id,
            "session_id": self.sink.session_id,
            "prompt": prompt,
            "reply": reply,
            "completed_at": time.time(),
        }
        self.reply_ready_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.reply_ready_dir / f"reply-ready-{message_id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)

    async def _renew(self, message_id: str) -> None:
        while not self._stopping:
            await asyncio.sleep(max(0.1, self.lease_seconds / 3))
            if not self.queue.renew_lease(message_id, self.owner, self.lease_seconds):
                return

    async def deliver_one(self) -> bool:
        row = self.queue.lease_next(
            owner=self.owner,
            target_session=self.sink.session_id,
            lease_seconds=self.lease_seconds,
        )
        if row is None:
            return False
        message_id = row["message_id"]
        self._status(state="delivering", active_message=message_id)
        renewer = asyncio.create_task(self._renew(message_id))
        try:
            # The sink's durable/event-history ledger closes the ordinary
            # acceptance/SQLite-ack crash window before any resubmission.
            accepted = self.queue.is_accepted(message_id, self.sink.session_id)
            if not accepted:
                accepted = message_id in await self.sink.accepted_ids()
            if not accepted:
                # Queue acceptance is also a voice interruption signal. The
                # typed-prompt hook remains in place and calls this controller.
                cancel_playback()
                await self.sink.submit(message_id, row["text"])
            if not self.queue.acknowledge(message_id, self.owner, self.sink.session_id):
                raise RuntimeError("lost lease before acknowledgement")
        except Exception as error:  # noqa: BLE001 - sink/SDK failures are retryable
            with suppress(RuntimeError):
                self.queue.fail(
                    message_id,
                    self.owner,
                    f"{type(error).__name__}: {error}",
                    max_attempts=self.max_attempts,
                )
            return True
        finally:
            renewer.cancel()
            with suppress(asyncio.CancelledError):
                await renewer

        async def track_completion() -> None:
            try:
                await self.sink.wait_completed(message_id)
                self.queue.complete(message_id, self.sink.session_id)
                reply_text_fn = getattr(self.sink, "reply_text", None)
                if reply_text_fn is not None:
                    reply = await reply_text_fn(message_id)
                    if reply:
                        self._write_reply_ready(message_id, row["text"], reply)
            except Exception as error:  # noqa: BLE001 - completion is best-effort
                # Acceptance is the delivery contract. Completion can be
                # reconciled later and must never cause prompt resubmission.
                self._status(
                    state="ready",
                    active_message=None,
                    last_error=f"completion: {type(error).__name__}: {error}",
                )

        task = asyncio.create_task(track_completion())
        self._completion_tasks.add(task)
        task.add_done_callback(self._completion_tasks.discard)
        self._status(state="ready", active_message=None)
        return True

    async def run(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            self.socket_path.unlink()
        notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        notifier.setblocking(False)
        notifier.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self._status(state="ready", active_message=None, last_error=None)
        loop = asyncio.get_running_loop()
        try:
            while not self._stopping:
                while await self.deliver_one():
                    pass
                try:
                    await asyncio.wait_for(
                        loop.sock_recv(notifier, 64), self.scan_interval
                    )
                except TimeoutError:
                    self._status(state="ready", active_message=None)
        finally:
            notifier.close()
            with suppress(FileNotFoundError):
                self.socket_path.unlink()
            if self._completion_tasks:
                await asyncio.gather(*self._completion_tasks, return_exceptions=True)
            await self.sink.close()
            self._status(state="stopped", active_message=None)

    def stop(self) -> None:
        self._stopping = True


class JsonlSink:
    """Deterministic test/diagnostic sink with a durable accepted-ID ledger."""

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def accepted_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        return {
            json.loads(line)["message_id"]
            for line in self.path.read_text().splitlines()
            if line.strip()
        }

    async def submit(self, message_id: str, text: str) -> None:
        if message_id in await self.accepted_ids():
            return
        record = json.dumps(
            {"message_id": message_id, "session_id": self.session_id, "text": text}
        )
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (record + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    async def wait_completed(self, message_id: str) -> None:
        return

    async def close(self) -> None:
        return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--session", required=True)
    parser.add_argument("--test-sink", type=Path)
    parser.add_argument("--scan-interval", type=float, default=0.1)
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    if args.test_sink:
        sink: DeliverySink = JsonlSink(args.test_sink, args.session)
    else:
        from claude_bridge import ClaudeSessionBridge

        sink = ClaudeSessionBridge(args.session)
        await sink.connect()
    worker = DeliveryWorker(
        VoiceQueue(args.db, args.socket),
        sink,
        socket_path=args.socket,
        scan_interval=args.scan_interval,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
