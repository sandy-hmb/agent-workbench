from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_evidence  # noqa: E402


DIGEST = "sha256:" + "a" * 64


def task_record(task_id: str = "T01") -> dict[str, object]:
    return {
        "kind": "taskEvidence",
        "taskId": task_id,
        "recordedAt": "2026-09-13T10:00:00Z",
        "repository": "service",
        "codeState": {"service": DIGEST},
        "checks": [
            {
                "type": "测试",
                "workingDirectory": "service",
                "command": "python3 -m unittest tests.test_service",
                "target": "tests/test_service.py",
                "executed": 1,
                "skipped": 0,
                "exitStatus": 0,
                "result": "目标检查通过",
            }
        ],
        "artifactRefs": [
            {
                "path": "artifacts/report.json",
                "sha256": DIGEST,
                "bytes": 12,
                "type": "json",
            }
        ],
        "validationKind": "行为",
        "deliveryCheck": "passed",
        "result": "passed",
    }


class WorkspaceEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.feature = Path(self.temp.name) / "features/demo"
        (self.feature / "testing").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_record_deduplicates_shared_objects_and_expands_references(self) -> None:
        first = workspace_evidence.record(self.feature, task_record("T01"))
        second = workspace_evidence.record(self.feature, task_record("T02"))

        objects = list((self.feature / "testing/evidence/objects").glob("*.json"))
        self.assertEqual(3, len(objects))
        self.assertTrue(first["changed"])
        self.assertTrue(second["changed"])
        store = workspace_evidence.load_store(self.feature)
        latest = store["latestByTask"]["T02"]
        self.assertEqual({"service": DIGEST}, latest["codeState"])
        self.assertEqual(
            "python3 -m unittest tests.test_service",
            latest["checks"][0]["command"],
        )
        self.assertEqual("artifacts/report.json", latest["artifactRefs"][0]["path"])

    def test_multiline_command_is_stored_as_inert_evidence(self) -> None:
        value = task_record()
        value["checks"][0]["command"] = "python3 -m unittest \\\n+  tests.test_service"
        workspace_evidence.record(self.feature, value)
        self.assertEqual(value["checks"][0]["command"], workspace_evidence.load_store(self.feature)["latestByTask"]["T01"]["checks"][0]["command"])

    def test_concurrent_writers_preserve_every_history_sequence(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: workspace_evidence.record(self.feature, task_record()), range(12)))
        self.assertEqual(list(range(1, 13)), sorted(item["sequence"] for item in results))
        self.assertEqual(12, workspace_evidence.history_page(self.feature)["total"])
        self.assertEqual(12, workspace_evidence.load_index(self.feature)["summary"]["records"])

    def test_missing_reference_and_hash_drift_fail_deterministically(self) -> None:
        workspace_evidence.record(self.feature, task_record())
        index = workspace_evidence.load_index(self.feature)
        task_ref = index["tasks"]["T01"]["latestObserved"]
        task_path = self.feature / "testing" / task_ref["path"]
        original = task_path.read_bytes()

        task_path.unlink()
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_REFERENCE_MISSING"):
            workspace_evidence.load_store(self.feature)

        task_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_HASH_MISMATCH"):
            workspace_evidence.load_store(self.feature)

    def test_symlink_and_path_traversal_are_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        evidence = self.feature / "testing/evidence"
        evidence.symlink_to(outside)
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_UNSAFE_PATH"):
            workspace_evidence.load_store(self.feature)
        evidence.unlink()

        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_UNSAFE_PATH"):
            workspace_evidence.safe_path(self.feature, "../outside.json")

    def test_index_corruption_is_not_silently_recovered(self) -> None:
        workspace_evidence.record(self.feature, task_record())
        index_path = self.feature / "testing/evidence/index.json"
        index_path.write_text(json.dumps({"schemaVersion": 2}), encoding="utf-8")

        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_INDEX_INVALID"):
            workspace_evidence.load_store(self.feature)

    def test_history_is_stable_and_cursor_is_bound_to_filter_and_revision(self) -> None:
        for number in range(1, 4):
            workspace_evidence.record(self.feature, task_record(f"T{number:02d}"))

        first = workspace_evidence.history_page(self.feature, limit=2)
        self.assertEqual([3, 2], [item["sequence"] for item in first["items"]])
        self.assertTrue(first["hasMore"])
        second = workspace_evidence.history_page(
            self.feature, cursor=first["nextCursor"], limit=2
        )
        self.assertEqual([1], [item["sequence"] for item in second["items"]])
        self.assertFalse(second["hasMore"])
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_CURSOR_INVALID"):
            workspace_evidence.history_page(
                self.feature, cursor=first["nextCursor"], task_id="T01", limit=2
            )
        invalid = workspace_evidence.encode_cursor(
            first["indexRevision"], 1, workspace_evidence.content_id({"task": None, "batch": None})
        )
        decoded = json.loads(__import__("base64").urlsafe_b64decode(invalid + "=" * (-len(invalid) % 4)))
        decoded["nextSequence"] = "1"
        malformed = __import__("base64").urlsafe_b64encode(
            json.dumps(decoded).encode()
        ).decode().rstrip("=")
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_CURSOR_INVALID"):
            workspace_evidence.history_page(self.feature, cursor=malformed)

    def test_explicit_task_lookup_returns_only_requested_record(self) -> None:
        workspace_evidence.record(self.feature, task_record())
        result = workspace_evidence.get_evidence(self.feature, task_id="T01")
        self.assertEqual("T01", result["taskId"])
        self.assertEqual("python3 -m unittest tests.test_service", result["checks"][0]["command"])
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_RECORD_NOT_FOUND"):
            workspace_evidence.get_evidence(self.feature, task_id="T99")

    def test_human_summary_is_deterministic_and_bounded(self) -> None:
        for number in range(1, 101):
            workspace_evidence.record(self.feature, task_record(f"T{number:03d}"))
        state = {
            "trustedProgress": {"completed": 100, "total": 100},
            "verificationPassed": False,
            "codeState": {"service": DIGEST},
            "blockers": [f"blocker-{number}" for number in range(20)],
            "artifacts": [],
        }
        first = workspace_evidence.render_summary(self.feature, state=state)
        second = workspace_evidence.render_summary(self.feature, state=state)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first.splitlines()), 200)
        self.assertIn("trustedProgress", first)
        self.assertIn("历史证据", first)


if __name__ == "__main__":
    unittest.main()
