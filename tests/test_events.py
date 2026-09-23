import unittest

from src import events as ev
from src.events import EventContractError, validate_event, require_valid
from src.store import EventStore


class EventContractTest(unittest.TestCase):
    def _base(self, **over):
        record = {
            "event_id": "e-1",
            "kind": ev.PURPOSE_REGISTERED,
            "occurred_at": "2026-09-23T10:00:00+08:00",
            "subject_id": "clinical-agent-release-governance",
            "version": 1,
            "actor": "reg",
            "payload": {
                "use_id": "U1",
                "agent": "a",
                "intended_use": "x",
                "risk_class": "STANDARD",
                "scope": {"department": "d", "population": "p", "device_protocol": "DEFAULT"},
                "key_subgroups": ["g1"],
                "thresholds": {"sensitivity": 0.9},
                "subgroup_thresholds": {"sensitivity": 0.9},
                "review_mode": "REQUIRED",
                "reviewer_role": "主治",
                "data_purpose": "目的",
                "min_sample": 100,
                "min_subgroup_sample": 30,
                "physician_signoff_required": True,
            },
        }
        record.update(over)
        return record

    def test_valid_event_passes(self):
        self.assertEqual(validate_event(self._base()), [])

    def test_unknown_kind_rejected(self):
        problems = validate_event(self._base(kind="NOT_A_KIND"))
        self.assertIn("unknown_kind", problems)

    def test_missing_payload_field(self):
        record = self._base()
        del record["payload"]["min_sample"]
        problems = validate_event(record)
        self.assertTrue(any(p == "payload_missing:min_sample" for p in problems))

    def test_phi_nested_in_subgroup_note_rejected(self):
        record = self._base()
        record["payload"]["scope"]["patient_name"] = "张三"
        problems = validate_event(record)
        self.assertTrue(any(p.startswith("phi_field:") for p in problems))

    def test_phi_in_list_rejected(self):
        record = self._base()
        record["payload"]["thresholds"] = [{"patient_id": "P-1"}]
        problems = validate_event(record)
        self.assertTrue(any(p.startswith("phi_field:") for p in problems))

    def test_case_ref_allowed_as_deidentified_identifier(self):
        record = self._base(kind=ev.ADVERSE_EVENT_REPORTED)
        record["payload"] = {
            "adverse_id": "AE-1",
            "release_id": "R1",
            "use_id": "U1",
            "case_ref": "CASE-0001",  # 去标识病例号：允许
            "severity": "严重",
            "summary": "漏报一例",
        }
        self.assertEqual(validate_event(record), [])

    def test_require_valid_raises(self):
        record = self._base()
        del record["payload"]["use_id"]
        with self.assertRaises(EventContractError):
            require_valid(record)

    def test_dup_event_id_and_version_conflict(self):
        store = EventStore()
        good = self._base()
        store.append(good)
        with self.assertRaises(ValueError):
            store.append(self._base(version=3))  # 期望 2
        with self.assertRaises(ValueError):
            store.append(self._base())  # event_id 重复


if __name__ == "__main__":
    unittest.main()
