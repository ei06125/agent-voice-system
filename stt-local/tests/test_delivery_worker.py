import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery_worker import DeliveryWorker, JsonlSink
from voice_queue import VoiceQueue


def queue_at(root: Path) -> VoiceQueue:
    return VoiceQueue(root / "queue.sqlite3", root / "delivery.sock")


def enqueue(q: VoiceQueue, n: int = 1, session: str = "s") -> list[str]:
    return [
        q.enqueue(
            capture_id="capture",
            utterance_seq=i,
            text=f"command {i}",
            is_shadow=False,
            target_session=session,
        ).message_id
        for i in range(1, n + 1)
    ]


def test_shadow_rows_are_never_leased():
    with tempfile.TemporaryDirectory() as tmp:
        q = queue_at(Path(tmp))
        q.enqueue(capture_id="c", utterance_seq=1, text="secret", is_shadow=True)
        assert q.lease_next(owner="w", target_session="s") is None


def test_expired_lease_recovers_and_backoff_dead_letters():
    with tempfile.TemporaryDirectory() as tmp:
        q = queue_at(Path(tmp))
        message_id = enqueue(q)[0]
        first = q.lease_next(owner="one", target_session="s", lease_seconds=1, now=10)
        assert first["message_id"] == message_id
        assert q.lease_next(owner="two", target_session="s", now=10.5) is None
        recovered = q.lease_next(owner="two", target_session="s", now=11.1)
        assert recovered["message_id"] == message_id
        assert q.fail(message_id, "two", "boom", max_attempts=2) == "dead_letter"
        assert q.replay_dead_letter(message_id)


def test_two_worker_race_has_one_winner():
    with tempfile.TemporaryDirectory() as tmp:
        q = queue_at(Path(tmp))
        message_id = enqueue(q)[0]
        barrier = threading.Barrier(3)
        results = []

        def lease(owner):
            barrier.wait()
            results.append(q.lease_next(owner=owner, target_session="s"))

        threads = [threading.Thread(target=lease, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        winners = [row for row in results if row]
        assert [row["message_id"] for row in winners] == [message_id]


def test_worker_orders_and_deduplicates_test_sink():
    async def scenario(root: Path):
        q = queue_at(root)
        ids = enqueue(q, 2)
        sink_path = root / "sink.jsonl"
        sink = JsonlSink(sink_path, "s")
        worker = DeliveryWorker(q, sink, socket_path=root / "delivery.sock")
        assert await worker.deliver_one()
        assert await worker.deliver_one()
        await asyncio.sleep(0)
        rows = [json.loads(line) for line in sink_path.read_text().splitlines()]
        assert [row["message_id"] for row in rows] == ids
        assert all(row["state"] == "completed" for row in q.list_messages())

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_acceptance_ack_crash_window_uses_sink_ledger():
    async def scenario(root: Path):
        q = queue_at(root)
        message_id = enqueue(q)[0]
        sink_path = root / "sink.jsonl"
        sink = JsonlSink(sink_path, "s")
        await sink.submit(message_id, "command 1")  # accepted, DB ack crashed
        worker = DeliveryWorker(q, sink, socket_path=root / "delivery.sock")
        assert await worker.deliver_one()
        assert len(sink_path.read_text().splitlines()) == 1

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


class ReplyingSink:
    """Test double for ClaudeSessionBridge's completion-signal contract:
    accepts/completes immediately and exposes reply_text() like the real
    SDK bridge does (JsonlSink above doesn't, and is used to prove the
    write is correctly skipped for sinks that lack it)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._accepted: set[str] = set()
        self._replies: dict[str, str] = {}

    async def accepted_ids(self) -> set[str]:
        return set(self._accepted)

    async def submit(self, message_id: str, text: str) -> None:
        self._accepted.add(message_id)
        self._replies[message_id] = f"reply to: {text}"

    async def wait_completed(self, message_id: str) -> None:
        return

    async def reply_text(self, message_id: str) -> str | None:
        return self._replies.pop(message_id, None)

    async def close(self) -> None:
        return


def test_reply_ready_file_written_on_completion():
    async def scenario(root: Path):
        q = queue_at(root)
        message_id = enqueue(q)[0]
        sink = ReplyingSink("s")
        worker = DeliveryWorker(
            q, sink, socket_path=root / "delivery.sock", reply_ready_dir=root
        )
        assert await worker.deliver_one()
        await asyncio.sleep(0)
        target = root / f"reply-ready-{message_id}.json"
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["message_id"] == message_id
        assert data["prompt"] == "command 1"
        assert data["reply"] == "reply to: command 1"

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_reply_ready_files_do_not_clobber_each_other():
    """Regression guard: one file per message_id, not a shared path - a
    shared path would let a fast second completion's os.replace silently
    overwrite a first reply the watcher hasn't consumed yet (same class of
    bug as the kokoro-local playback-flag clobber fixed earlier)."""

    async def scenario(root: Path):
        q = queue_at(root)
        ids = enqueue(q, 2)
        sink = ReplyingSink("s")
        worker = DeliveryWorker(
            q, sink, socket_path=root / "delivery.sock", reply_ready_dir=root
        )
        assert await worker.deliver_one()
        assert await worker.deliver_one()
        await asyncio.sleep(0)
        for message_id in ids:
            assert (root / f"reply-ready-{message_id}.json").exists()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_reply_ready_file_skipped_for_sink_without_reply_text():
    async def scenario(root: Path):
        q = queue_at(root)
        message_id = enqueue(q)[0]
        sink = JsonlSink(root / "sink.jsonl", "s")
        worker = DeliveryWorker(
            q, sink, socket_path=root / "delivery.sock", reply_ready_dir=root
        )
        assert await worker.deliver_one()
        await asyncio.sleep(0)
        assert not (root / f"reply-ready-{message_id}.json").exists()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_socket_receiver_wakes_without_waiting_for_scan():
    async def scenario(root: Path):
        q = queue_at(root)
        sink = JsonlSink(root / "sink.jsonl", "s")
        worker = DeliveryWorker(
            q, sink, socket_path=root / "delivery.sock", scan_interval=5
        )
        task = asyncio.create_task(worker.run())
        deadline = time.monotonic() + 1
        while not (root / "delivery.sock").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        enqueue(q)
        q.notify()
        deadline = time.monotonic() + 1
        while not (root / "sink.jsonl").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        worker.stop()
        q.notify()
        await task
        assert (root / "sink.jsonl").exists()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
