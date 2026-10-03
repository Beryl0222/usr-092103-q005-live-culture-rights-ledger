import json
import unittest
from pathlib import Path

from src import contracts as C
from src.event_store import EventStore
from src.validator import validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_schema_enums_match_code(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["$defs"]["event_type"]["enum"]), set(C.EVENT_TYPES))
        self.assertEqual(set(schema["$defs"]["aggregate_type"]["enum"]),
                         set(C.AGGREGATE_TYPES))

    def test_sample_stream_events_pass_envelope(self) -> None:
        store = EventStore.load_jsonl(ROOT / "data" / "sample_events.jsonl")
        for event in store.events():
            self.assertEqual(validate_event(event), [])
        self.assertGreater(len(store), 0)

    def test_restriction_reasons_in_schema(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        allowed = set(schema["$defs"]["restriction_reason"]["enum"])
        self.assertTrue(set(C.RESTRICTION_REASONS) <= allowed)


if __name__ == "__main__":
    unittest.main()
