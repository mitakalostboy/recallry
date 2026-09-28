from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from recallry.db import Database

from test_recallry import VALID_CONFIG


class MetricsCliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "recallry"
        self.project = base / "project"
        self.root.mkdir()
        self.project.mkdir()
        (self.root / "config.toml").write_text(VALID_CONFIG)
        (self.project / ".recallry.toml").write_text('project_id = "sample-project"\n')
        self.source_root = Path(__file__).resolve().parents[1] / "src"
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.database = Database(self.root / "data" / "recallry.db")
        item = self.database.add(
            title="Reusable rule", content="verified content", scope="project",
            project="sample-project", category="rule",
        )
        self.verified, _ = self.database.transition(item.id, "promote")

    def tearDown(self):
        self.temp.cleanup()

    @property
    def metrics_path(self) -> Path:
        return self.root / "data" / "metrics.db"

    def run_cli(
        self, *arguments: str, readonly: bool = False, metrics: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.source_root)
        environment.pop("RECALLRY_READONLY", None)
        environment.pop("RECALLRY_METRICS", None)
        if readonly:
            environment["RECALLRY_READONLY"] = "1"
        if metrics is not None:
            environment["RECALLRY_METRICS"] = metrics
        return subprocess.run(
            [sys.executable, "-m", "recallry", "--root", str(self.root), *arguments],
            text=True, capture_output=True, env=environment, check=False,
        )

    def automatic(self, *, readonly: bool = True, metrics: str | None = None,
                  task: str = "private task text verified"):
        return self.run_cli(
            "context", "--project-root", str(self.project), "--task", task,
            "--automatic", "--format", "json", readonly=readonly, metrics=metrics,
        )

    def test_metrics_default_off_has_no_hidden_write(self):
        before = self.database.path.read_bytes()
        result = self.automatic(readonly=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.metrics_path.exists())
        self.assertEqual(self.database.path.read_bytes(), before)

    def test_false_and_invalid_values_are_off(self):
        for value in ("0", "false", "no", "unexpected"):
            with self.subTest(value=value):
                result = self.automatic(metrics=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.metrics_path.exists())

    def test_readonly_with_explicit_metrics_writes_only_sidecar(self):
        before = self.database.path.read_bytes()
        use_count = self.database.get(self.verified.id).use_count
        result = self.automatic(readonly=True, metrics="yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.metrics_path.is_file())
        self.assertEqual(self.database.path.read_bytes(), before)
        self.assertEqual(self.database.get(self.verified.id).use_count, use_count)
        with sqlite3.connect(self.metrics_path) as connection:
            event = connection.execute(
                "SELECT event_type,project_id,command,mode,included_count,injected_chars,truncated "
                "FROM metrics_events"
            ).fetchone()
            ids = [row[0] for row in connection.execute(
                "SELECT knowledge_id FROM metrics_event_knowledge")]
        self.assertEqual(event[:4], ("automatic_context", "sample-project", "context", "automatic"))
        self.assertEqual(event[4], 1)
        self.assertGreater(event[5], 0)
        self.assertIn(event[6], (0, 1))
        self.assertEqual(ids, [self.verified.id])

    def test_metrics_failure_is_fail_open(self):
        self.metrics_path.mkdir()
        result = self.automatic(readonly=True, metrics="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")

    def test_add_promote_reject_events_and_noop_transition(self):
        add = self.run_cli(
            "add", "--title", "Candidate", "--content", "safe content",
            "--scope", "project", "--project", "sample-project",
            "--source", "agent-suggested automatic-candidate", metrics="true",
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        candidate_id = add.stdout.split()[1]
        promote = self.run_cli("promote", candidate_id, metrics="true")
        self.assertEqual(promote.returncode, 0, promote.stderr)
        noop = self.run_cli("promote", candidate_id, metrics="true")
        self.assertEqual(noop.returncode, 0, noop.stderr)
        rejected = self.run_cli(
            "add", "--title", "Reject me", "--content", "safe",
            "--scope", "project", "--project", "other-project", metrics="true",
        )
        rejected_id = rejected.stdout.split()[1]
        self.assertEqual(self.run_cli("reject", rejected_id, metrics="true").returncode, 0)
        with sqlite3.connect(self.metrics_path) as connection:
            events = connection.execute(
                "SELECT event_type,project_id,origin FROM metrics_events ORDER BY id"
            ).fetchall()
        self.assertEqual(events, [
            ("candidate_created", "sample-project", "automatic"),
            ("knowledge_promoted", "sample-project", None),
            ("candidate_created", "other-project", "manual"),
            ("knowledge_rejected", "other-project", None),
        ])

    def test_stats_aggregation_filters_json_and_does_not_create_db(self):
        missing = self.run_cli("stats", "--json", readonly=True)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertFalse(self.metrics_path.exists())
        self.assertIsNone(json.loads(missing.stdout)["metrics"]["collection_since"])

        second = self.database.add(
            title="Second reusable rule", content="second verified content",
            scope="project", project="sample-project", category="rule",
        )
        self.database.transition(second.id, "promote")
        self.assertEqual(self.automatic(metrics="1", task="verified content").returncode, 0)
        other_project = self.project.parent / "other"
        other_project.mkdir()
        (other_project / ".recallry.toml").write_text('project_id = "other-project"\n')
        result = self.run_cli(
            "context", "--project-root", str(other_project), "--task", "task",
            "--automatic", "--format", "json", metrics="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        all_stats = json.loads(self.run_cli("stats", "--json").stdout)
        self.assertEqual(all_stats["metrics"]["automatic_reads"], 2)
        self.assertEqual(all_stats["metrics"]["retrievals"], 2)
        self.assertEqual(all_stats["metrics"]["unique_used"], 2)
        self.assertEqual(all_stats["metrics"]["projects_active"], 2)
        self.assertGreater(all_stats["metrics"]["estimated_injected_tokens"], 0)
        project_stats = json.loads(self.run_cli(
            "stats", "--project", "sample-project", "--days", "7", "--json"
        ).stdout)
        self.assertEqual(project_stats["metrics"]["automatic_reads"], 1)
        self.assertEqual(project_stats["metrics"]["retrievals"], 2)
        self.assertEqual(project_stats["filters"], {"project": "sample-project", "days": 7})

    def test_metrics_schema_contains_no_prompt_or_content_fields(self):
        secret_task = "do-not-store-this-task"
        self.assertEqual(self.automatic(metrics="1", task=secret_task).returncode, 0)
        with sqlite3.connect(self.metrics_path) as connection:
            columns = {
                row[1] for table in ("metrics_events", "metrics_event_knowledge")
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            stored_text = " ".join(
                str(value) for row in connection.execute("SELECT * FROM metrics_events")
                for value in row if value is not None
            )
        self.assertTrue({"project_id", "knowledge_id", "injected_chars"}.issubset(columns))
        self.assertTrue({"prompt", "task", "content", "response"}.isdisjoint(columns))
        self.assertNotIn(secret_task, stored_text)


if __name__ == "__main__":
    unittest.main()
