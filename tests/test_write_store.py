"""Tests for the durable two-phase write record store.

Run from the x-starter directory:

    python -X utf8 -m unittest tests.test_write_store -v
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import write_store  # noqa: E402
from write_store import STATES, TERMINAL, WriteStore, WriteStoreError  # noqa: E402

BODY = "SECRET-BODY-MARKER the post body that must never be persisted"
PHRASE = "PUBLISH DEADBEEF"
AUDIT_KEYS = {"seq", "at", "write_id", "from", "to", "prev", "hash", "text_sha256", "reason"}


class FakeClock:
    """Deterministic clock so timestamps and retention are exact."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_record(clock: FakeClock, write_id: str, *, ttl: float = 180.0, **extra: object) -> dict:
    record = {
        "write_id": write_id,
        "session_id": "sess-1",
        "account_ref": "acct-1",
        "text": BODY,
        "text_sha256": hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
        "confirmation_phrase": PHRASE,
        "expires_epoch": clock.now + ttl,
    }
    record.update(extra)
    return record


class WriteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.path = self.root / "state" / "writes.json"
        self.clock = FakeClock()

    def store(self, path: Path | None = None, **kwargs: object) -> WriteStore:
        target = self.path if path is None else path
        kwargs.setdefault("clock", self.clock)
        return WriteStore(target, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------- basics

    def test_create_and_get_roundtrip(self) -> None:
        store = self.store()
        created = store.create(make_record(self.clock, "w1"))
        self.assertEqual(created["state"], "prepared")
        self.assertEqual(created["write_id"], "w1")
        self.assertTrue(created["created_at"].endswith("Z"))
        self.assertEqual(created["created_at"], created["updated_at"])
        loaded = store.get("w1")
        assert loaded is not None
        self.assertEqual(loaded["account_ref"], "acct-1")
        self.assertEqual(loaded["confirmation_phrase"], PHRASE)
        self.assertIsNone(store.get("missing"))
        # The directory and file are created on first write.
        self.assertTrue(self.path.exists())
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["version"], 1)
        self.assertEqual(list(on_disk["records"]), ["w1"])

    def test_get_returns_deep_copy(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1", payload={"nested": {"depth": 1}}))
        first = store.get("w1")
        assert first is not None
        first["state"] = "completed"
        first["payload"]["nested"]["depth"] = 99
        second = store.get("w1")
        assert second is not None
        self.assertEqual(second["state"], "prepared")
        self.assertEqual(second["payload"]["nested"]["depth"], 1)
        self.assertEqual(store.stats()["prepared"], 1)

    def test_transition_moves_through_states(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        self.clock.advance(5)
        running = store.transition("w1", "prepared", "executing")
        self.assertEqual(running["state"], "executing")
        self.assertNotEqual(running["updated_at"], running["created_at"])
        self.clock.advance(5)
        done = store.transition("w1", ("executing",), "completed", result={"id": "123"})
        self.assertEqual(done["state"], "completed")
        self.assertEqual(done["result"], {"id": "123"})
        counts = store.stats()
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["total"], 1)
        self.assertEqual(set(counts) - {"total", "recovered_from_corruption"}, set(STATES))

    def test_transition_conflict_and_missing_record(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        store.transition("w1", "prepared", "completed", result={"id": "1"})
        with self.assertRaises(WriteStoreError) as caught:
            store.transition("w1", "prepared", "executing")
        self.assertEqual(caught.exception.code, "WRITE_STATE_CONFLICT")
        self.assertEqual(caught.exception.details["state"], "completed")
        with self.assertRaises(WriteStoreError) as missing:
            store.transition("nope", "prepared", "executing")
        self.assertEqual(missing.exception.code, "WRITE_NOT_FOUND")
        with self.assertRaises(ValueError):
            store.transition("w1", "completed", "not-a-state")
        self.assertEqual(store.get("w1")["state"], "completed")  # type: ignore[index]

    def test_duplicate_write_id_is_rejected(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        with self.assertRaises(WriteStoreError) as caught:
            store.create(make_record(self.clock, "w1"))
        self.assertEqual(caught.exception.code, "WRITE_STATE_CONFLICT")
        self.assertEqual(len(store.list()), 1)

    # ----------------------------------------------------------- restarts

    def test_restart_marks_executing_uncertain(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        store.transition("w1", "prepared", "executing")
        self.clock.advance(30)
        restarted = self.store()
        record = restarted.get("w1")
        assert record is not None
        self.assertEqual(record["state"], "uncertain")
        self.assertEqual(record["recovery_reason"], "process_restarted")
        events = restarted.audit(limit=50)
        self.assertTrue(events["chain_ok"])
        recovery = [event for event in events["events"] if event["to"] == "uncertain"]
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["from"], "executing")
        self.assertEqual(recovery[0]["reason"], "process_restarted")
        # The recovery is durable: a third process sees it too.
        third = self.store()
        assert third.get("w1") is not None
        self.assertEqual(third.get("w1")["state"], "uncertain")  # type: ignore[index]
        self.assertTrue(third.audit()["chain_ok"])

    def test_restart_keeps_prepared_confirmable(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        self.clock.advance(20)
        restarted = self.store()
        record = restarted.get("w1")
        assert record is not None
        self.assertEqual(record["state"], "prepared")
        second = store.create(make_record(self.clock, "w2"))
        self.assertEqual(second["state"], "prepared")
        confirmed = restarted.transition("w1", "prepared", "completed", result={"id": "7"})
        self.assertEqual(confirmed["state"], "completed")

    def test_completed_record_survives_restart(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        store.transition("w1", "prepared", "executing")
        store.transition("w1", "executing", "completed", result={"id": "9001"})
        restarted = self.store()
        record = restarted.get("w1")
        assert record is not None
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["result"], {"id": "9001"})
        self.assertEqual(restarted.stats()["completed"], 1)

    def test_terminal_records_are_pruned_after_retention(self) -> None:
        store = self.store(retention_seconds=10)
        store.create(make_record(self.clock, "old"))
        store.transition("old", "prepared", "failed")
        store.create(make_record(self.clock, "active"))
        self.clock.advance(60)
        restarted = self.store(retention_seconds=10)
        self.assertIsNone(restarted.get("old"))
        self.assertIsNotNone(restarted.get("active"))
        self.assertEqual(restarted.stats()["total"], 1)
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(persisted["records"]), ["active"])

    # ------------------------------------------------------------- expiry

    def test_expire_due_moves_only_prepared_records(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "short", ttl=10))
        store.create(make_record(self.clock, "long", ttl=600))
        store.create(make_record(self.clock, "running"))
        store.transition("running", "prepared", "executing")
        self.clock.advance(11)
        self.assertEqual(store.expire_due(), 1)
        self.assertEqual(store.expire_due(), 0)
        self.assertEqual(store.get("short")["state"], "expired")  # type: ignore[index]
        self.assertEqual(store.get("long")["state"], "prepared")  # type: ignore[index]
        self.assertEqual(store.get("running")["state"], "executing")  # type: ignore[index]
        self.assertEqual(store.stats()["expired"], 1)
        restarted = self.store()
        self.assertEqual(restarted.get("short")["state"], "expired")  # type: ignore[index]

    # -------------------------------------------------------------- audit

    def test_audit_chain_is_verifiable_and_newest_last(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        self.clock.advance(1)
        store.transition("w1", "prepared", "executing")
        self.clock.advance(1)
        store.transition("w1", "executing", "completed", result={"id": "5"})
        audit = store.audit(limit=50)
        self.assertTrue(audit["chain_ok"])
        events = audit["events"]
        self.assertEqual([event["to"] for event in events], ["prepared", "executing", "completed"])
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertEqual(audit["head"], events[-1]["hash"])
        self.assertEqual(events[0]["prev"], "0" * 64)
        self.assertEqual(events[2]["prev"], events[1]["hash"])
        self.assertEqual(store.audit(limit=1)["events"], [events[-1]])
        self.assertEqual(store.audit(limit=0)["events"], [])

    def test_audit_detects_tampering(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        store.transition("w1", "prepared", "completed", result={"id": "1"})
        self.assertTrue(store.audit()["chain_ok"])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["audit"][0]["to"] = "failed"
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        tampered = self.store()
        self.assertFalse(tampered.audit()["chain_ok"])
        # A record edit alone is visible through the audit chain as well.
        self.assertEqual(tampered.get("w1")["state"], "completed")  # type: ignore[index]

    def test_audit_never_records_text_or_confirmation_phrase(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        store.transition("w1", "prepared", "executing")
        store.transition("w1", "executing", "uncertain", reason="x_network")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        audit_text = json.dumps(payload["audit"], ensure_ascii=False)
        self.assertNotIn(BODY, audit_text)
        self.assertNotIn(PHRASE, audit_text)
        self.assertIn(hashlib.sha256(BODY.encode("utf-8")).hexdigest(), audit_text)
        # The audit log keeps only the digest; the record keeps what is needed
        # to execute the confirmed write.
        self.assertEqual(payload["records"]["w1"]["text"], BODY)
        for event in store.audit()["events"]:
            self.assertTrue(set(event) <= AUDIT_KEYS, set(event) - AUDIT_KEYS)
            self.assertNotIn("text", event)
            self.assertNotIn("confirmation_phrase", event)
        events = store.audit()["events"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]["reason"], "x_network")

    def test_audit_truncation_keeps_chain_verifiable(self) -> None:
        original = write_store.AUDIT_MAX_EVENTS
        write_store.AUDIT_MAX_EVENTS = 4
        try:
            store = self.store()
            for index in range(6):
                self.clock.advance(1)
                store.create(make_record(self.clock, f"w{index}"))
            audit = store.audit(limit=50)
            self.assertEqual(len(audit["events"]), 4)
            self.assertTrue(audit["chain_ok"])
            self.assertEqual([event["seq"] for event in audit["events"]], [3, 4, 5, 6])
            self.assertEqual(audit["head"], audit["events"][-1]["hash"])
            persisted = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted["audit"]), 4)
        finally:
            write_store.AUDIT_MAX_EVENTS = original

    # ------------------------------------------------------------ safety

    def test_credential_like_fields_are_rejected(self) -> None:
        store = self.store()
        with self.assertRaises(ValueError):
            store.create(make_record(self.clock, "bad", access_token="abc"))
        with self.assertRaises(ValueError):
            store.create(make_record(self.clock, "bad2", headers={"Authorization": "Bearer x"}))
        with self.assertRaises(ValueError):
            store.create(make_record(self.clock, "bad3", nested={"client_secret": "x"}))
        self.assertEqual(store.stats()["total"], 0)
        self.assertFalse(self.path.exists())
        store.create(make_record(self.clock, "w1"))
        with self.assertRaises(ValueError):
            store.transition("w1", "prepared", "executing", refresh_token="abc")
        self.assertEqual(store.get("w1")["state"], "prepared")  # type: ignore[index]

    def test_corrupt_file_is_quarantined_and_recovered(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        broken = '{"version": 1, "records": {"w1": '
        self.path.write_text(broken, encoding="utf-8")
        store = self.store()
        stats = store.stats()
        self.assertEqual(stats["recovered_from_corruption"], 1)
        self.assertEqual(stats["total"], 0)
        corrupt = sorted(self.path.parent.glob(f"{self.path.name}.corrupt-*"))
        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0].read_text(encoding="utf-8"), broken)
        store.create(make_record(self.clock, "w1"))
        restarted = self.store()
        self.assertIsNotNone(restarted.get("w1"))
        self.assertNotIn("recovered_from_corruption", restarted.stats())

    def test_max_records_limit_then_pruning_frees_space(self) -> None:
        store = self.store(max_records=3, retention_seconds=30)
        for index in range(3):
            self.clock.advance(1)
            store.create(make_record(self.clock, f"w{index}"))
        with self.assertRaises(WriteStoreError) as caught:
            store.create(make_record(self.clock, "w3"))
        self.assertEqual(caught.exception.code, "WRITE_STORE_FULL")
        self.assertEqual(caught.exception.details["max_records"], 3)
        store.transition("w0", "prepared", "failed")
        self.clock.advance(120)
        store.create(make_record(self.clock, "w3"))
        self.assertEqual(store.stats()["total"], 3)
        self.assertIsNone(store.get("w0"))

    def test_concurrent_transition_has_exactly_one_winner(self) -> None:
        store = self.store()
        store.create(make_record(self.clock, "w1"))
        workers = 20
        barrier = threading.Barrier(workers)
        results: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            try:
                store.transition("w1", "prepared", "executing")
                outcome = "ok"
            except WriteStoreError as exc:
                outcome = exc.code
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), workers)
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("WRITE_STATE_CONFLICT"), workers - 1)
        self.assertEqual(store.get("w1")["state"], "executing")  # type: ignore[index]
        completed_events = [event for event in store.audit(limit=100)["events"] if event["to"] == "executing"]
        self.assertEqual(len(completed_events), 1)

    def test_memory_only_mode_uses_no_disk(self) -> None:
        store = WriteStore(None, clock=self.clock)
        store.create(make_record(self.clock, "w1"))
        store.transition("w1", "prepared", "executing")
        self.assertEqual(store.get("w1")["state"], "executing")  # type: ignore[index]
        self.assertEqual(store.stats()["total"], 1)
        self.assertTrue(store.audit()["chain_ok"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_write_leaves_no_temporary_files(self) -> None:
        store = self.store()
        for index in range(5):
            store.create(make_record(self.clock, f"w{index}"))
            store.transition(f"w{index}", "prepared", "executing")
        leftovers = [path.name for path in self.path.parent.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual([path.name for path in self.root.iterdir()], ["state"])

    def test_list_is_newest_first_and_session_scoped(self) -> None:
        store = self.store()
        for index in range(4):
            store.create(make_record(self.clock, f"w{index}", session_id="sess-1"))
        store.create(make_record(self.clock, "other", session_id="sess-2"))
        listing = store.list()
        self.assertEqual(len(listing), 5)
        self.assertEqual(listing[0]["write_id"], "other")
        self.assertEqual([item["write_id"] for item in listing[1:]], ["w3", "w2", "w1", "w0"])
        scoped = store.list(session_id="sess-1")
        self.assertEqual([item["write_id"] for item in scoped], ["w3", "w2", "w1", "w0"])
        self.assertEqual([item["write_id"] for item in store.list(limit=2)], ["other", "w3"])
        self.assertEqual(store.list(limit=0), [])
        self.assertEqual(os.path.abspath(self.path), os.path.abspath(store._path))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
