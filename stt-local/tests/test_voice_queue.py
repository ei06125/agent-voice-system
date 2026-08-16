import os
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from voice_queue import DuplicateEventError, VoiceQueue


def test_enqueue_is_durable_and_ordered():
    with tempfile.TemporaryDirectory() as state_dir:
        db_path = Path(state_dir) / "queue.sqlite3"
        queue = VoiceQueue(db_path, Path(state_dir) / "delivery.sock")

        first = queue.enqueue(
            capture_id="capture-a", utterance_seq=1, text="first command"
        )
        second = queue.enqueue(
            capture_id="capture-a", utterance_seq=2, text="second command"
        )

        reopened = VoiceQueue(db_path, Path(state_dir) / "delivery.sock")
        messages = reopened.list_messages(include_text=True)
        assert [message["message_id"] for message in messages] == [
            first.message_id,
            second.message_id,
        ]
        assert [message["text"] for message in messages] == [
            "first command",
            "second command",
        ]
        assert all(message["state"] == "pending" for message in messages)
        assert all(message["is_shadow"] == 1 for message in messages)


def test_repeat_producer_event_is_idempotent():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3")

        first = queue.enqueue(
            capture_id="capture-a",
            utterance_seq=1,
            text="same command",
            message_id="message-a",
        )
        repeated = queue.enqueue(
            capture_id="capture-a",
            utterance_seq=1,
            text="same command",
            message_id="message-a",
        )

        assert first.inserted is True
        assert repeated.inserted is False
        assert repeated.message_id == first.message_id
        assert len(queue.list_messages()) == 1


def test_same_event_with_different_content_is_rejected():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3")
        queue.enqueue(capture_id="capture-a", utterance_seq=1, text="first command")

        try:
            queue.enqueue(
                capture_id="capture-a", utterance_seq=1, text="changed command"
            )
        except DuplicateEventError:
            pass
        else:
            raise AssertionError("changed duplicate producer event was accepted")

        assert len(queue.list_messages()) == 1


def test_identical_text_from_distinct_events_is_preserved():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3")
        first = queue.enqueue(
            capture_id="capture-a", utterance_seq=1, text="repeat this"
        )
        second = queue.enqueue(
            capture_id="capture-b", utterance_seq=1, text="repeat this"
        )

        assert first.message_id != second.message_id
        assert len(queue.list_messages()) == 2


def test_commit_survives_missing_socket_notification():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(
            Path(state_dir) / "queue.sqlite3",
            Path(state_dir) / "missing.sock",
        )

        result = queue.enqueue_and_notify(
            capture_id="capture-a", utterance_seq=1, text="durable command"
        )

        assert result.inserted is True
        assert len(queue.list_messages()) == 1


def test_notification_is_sent_after_commit():
    with tempfile.TemporaryDirectory() as state_dir:
        socket_path = Path(state_dir) / "delivery.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(socket_path))
        server.settimeout(1)
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3", socket_path)

        result = queue.enqueue_and_notify(
            capture_id="capture-a", utterance_seq=1, text="wake worker"
        )
        notification = server.recv(1)
        server.close()

        assert result.inserted is True
        assert notification == b"1"
        assert len(queue.list_messages()) == 1


def test_concurrent_duplicate_insert_creates_one_row():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3")
        results = []

        def insert() -> None:
            results.append(
                queue.enqueue(
                    capture_id="capture-a",
                    utterance_seq=1,
                    text="one command",
                    message_id="message-a",
                )
            )

        threads = [threading.Thread(target=insert) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(result.inserted for result in results) == 1
        assert len(queue.list_messages()) == 1


def test_database_and_parent_permissions_are_private():
    with tempfile.TemporaryDirectory() as parent_dir:
        state_dir = Path(parent_dir) / "state"
        db_path = state_dir / "queue.sqlite3"
        VoiceQueue(db_path)

        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_transaction_rollback_leaves_no_partial_row():
    with tempfile.TemporaryDirectory() as state_dir:
        db_path = Path(state_dir) / "queue.sqlite3"
        queue = VoiceQueue(db_path)

        connection = sqlite3.connect(db_path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO messages (
                message_id, capture_id, utterance_seq, created_at,
                finalized_at, text, is_shadow
            ) VALUES ('partial', 'capture-a', 1, 1, 1, 'partial', 1)
            """
        )
        connection.execute("ROLLBACK")
        connection.close()

        assert queue.list_messages() == []


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"PASS {name}")
