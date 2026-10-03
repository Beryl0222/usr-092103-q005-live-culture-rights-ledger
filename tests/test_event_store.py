import unittest

from src import contracts as C
from src.event_store import EventStore


def _event(version, agg="a1", at="2026-09-20T19:30:00+08:00", etype="SESSION_STARTED",
           agg_type="live_session", eid=None):
    return C.make_event(etype, agg_type, agg, version, "测试事件",
                        event_id=eid or f"e-{agg}-{version}", occurred_at=at)


class EventStoreTest(unittest.TestCase):
    def test_append_requires_sequential_version(self):
        store = EventStore()
        store.append(_event(1))
        with self.assertRaises(C.ContractError):
            store.append(_event(3))
        with self.assertRaises(C.ContractError):
            store.append(_event(1))  # 跳回旧版本等同改写
        store.append(_event(2))
        self.assertEqual([e["version"] for e in store.events()], [1, 2])

    def test_event_id_unique(self):
        store = EventStore()
        store.append(_event(1, agg="a1", eid="dup"))
        with self.assertRaises(C.ContractError):
            store.append(_event(1, agg="a2", eid="dup"))

    def test_time_cannot_go_backwards_within_aggregate(self):
        store = EventStore()
        store.append(_event(1, at="2026-09-20T20:00:00+08:00"))
        with self.assertRaises(C.ContractError):
            store.append(_event(2, at="2026-09-20T19:00:00+08:00"))

    def test_events_are_returned_as_copies(self):
        store = EventStore()
        store.append(_event(1))
        snap = store.events()
        snap[0]["summary"] = "篡改"
        self.assertEqual(store.events()[0]["summary"], "测试事件")

    def test_jsonl_roundtrip(self):
        import tempfile
        from pathlib import Path
        store = EventStore()
        store.append(_event(1))
        store.append(_event(2))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            store.save_jsonl(path)
            loaded = EventStore.load_jsonl(path)
        self.assertEqual(len(loaded), 2)

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(C.ContractError):
            C.make_event("NOT_A_THING", "live_session", "x", 1, "x")


if __name__ == "__main__":
    unittest.main()
