import json
import unittest
from pathlib import Path

from src.contract import parse_timestamp, validate


class ContractTest(unittest.TestCase):
    def test_sample(self):
        data = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "event.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate(data), [])
        # 示例同时满足完整领域事件合同（信封 + 负载 + PHI 边界）。
        from src.events import validate_event

        self.assertEqual(validate_event(data), [])

    def test_rejects_missing_fields(self):
        problems = validate({"event_id": "x", "kind": "K"})
        self.assertIn("occurred_at", problems)
        self.assertIn("subject_id", problems)
        self.assertIn("version", problems)

    def test_rejects_naive_timestamp(self):
        record = {
            "event_id": "e1",
            "kind": "K",
            "occurred_at": "2026-09-23T10:00:00",
            "subject_id": "s",
            "version": 1,
        }
        self.assertIn("occurred_at_not_iso8601", validate(record))

    def test_accepts_offset_and_z(self):
        base = {
            "event_id": "e1",
            "kind": "K",
            "subject_id": "s",
            "version": 1,
        }
        self.assertEqual(validate({**base, "occurred_at": "2026-09-23T10:00:00+08:00"}), [])
        self.assertEqual(validate({**base, "occurred_at": "2026-09-23T02:00:00Z"}), [])

    def test_rejects_negative_version_and_unknown_fields(self):
        record = {
            "event_id": "e1",
            "kind": "K",
            "occurred_at": "2026-09-23T10:00:00+08:00",
            "subject_id": "s",
            "version": -1,
            "rogue": 1,
        }
        problems = validate(record)
        self.assertIn("version_negative", problems)
        self.assertTrue(any(p.startswith("unknown_fields") for p in problems))

    def test_parse_timestamp_normalized(self):
        self.assertEqual(
            parse_timestamp("2026-09-23T02:00:00Z").utcoffset().total_seconds(), 0
        )


if __name__ == "__main__":
    unittest.main()
