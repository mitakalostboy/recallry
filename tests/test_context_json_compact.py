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


class CompactContextCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "store"
        self.project = base / "project"
        self.project.mkdir()
        (self.project / ".recallry.toml").write_text('project_id = "sample-project"\n')
        self.db = Database(self.root / "data" / "recallry.db")
        self.assertEqual(self.cli("init").returncode, 0)

    def cli(self, *args, metrics=False):
        env = os.environ.copy()
        for name in ("RECALLRY_HOME", "RECALLRY_READONLY", "RECALLRY_METRICS"):
            env.pop(name, None)
        env["RECALLRY_METRICS"] = "1" if metrics else "0"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "recallry", "--root", str(self.root), *args],
            text=True, capture_output=True, env=env, check=False,
        )

    def context(self, format_name=None, *, automatic=True, task="確認", metrics=False, extra=()):
        args = ["context", "--project-root", str(self.project), "--task", task]
        if automatic:
            args.append("--automatic")
        args.extend(extra)
        if format_name is not None:
            args.extend(("--format", format_name))
        return self.cli(*args, metrics=metrics)

    def add_verified(self, title, content, *, scope="global", project=None):
        item = self.db.add(title=title, content=content, scope=scope,
                           project=project, category="rule", status="candidate")
        self.db.transition(item.id, "promote")
        return item.id

    def paired(self, **kwargs):
        legacy = self.context("json", **kwargs)
        compact = self.context("json-compact", **kwargs)
        self.assertEqual((legacy.returncode, compact.returncode), (0, 0))
        full = json.loads(legacy.stdout)
        expected = json.loads(legacy.stdout)
        for item in expected["knowledge"]:
            del item["content"]
        self.assertEqual(json.loads(compact.stdout), expected)
        self.assertEqual(compact.stdout, json.dumps(expected, ensure_ascii=False) + "\n")
        self.assertEqual(expected["included_chars"], len(expected["markdown"]))
        return full, expected

    def test_help_default_markdown_empty_and_truncation(self):
        self.assertIn("json-compact", self.cli("context", "--help").stdout)
        full, compact = self.paired()
        self.assertEqual(full["knowledge"], [])
        self.assertEqual(compact["included_count"], 0)
        for index in range(12):
            self.add_verified(f"長い項目 {index}", "日本語" * 500)
        full, compact = self.paired(task="長い", extra=("--limit", "999"))
        self.assertTrue(compact["truncated"])
        self.assertLessEqual(compact["included_count"], 8)
        self.assertLessEqual(compact["included_chars"], 6000)
        self.assertEqual(self.context().stdout, self.context("markdown").stdout)
        self.assertEqual(self.context(task="長い", extra=("--limit", "999")).stdout,
                         full["markdown"])

    def test_bodies_metadata_order_scope_and_verified_only(self):
        first = self.add_verified('同名 "規則"', "漢字\n改行 \\ path", scope="project",
                                  project="sample-project")
        second = self.add_verified('同名 "規則"', "別の本文", scope="project",
                                   project="sample-project")
        global_id = self.add_verified("確認 global", 'global "text"')
        self.add_verified("別プロジェクト", "除外", scope="project", project="other-project")
        self.db.add(title="未検証", content="除外", scope="global", project=None,
                    category="rule", status="candidate")
        full, compact = self.paired(task="同名 確認")
        self.assertEqual({x["id"] for x in compact["knowledge"]}, {first, second, global_id})
        self.assertEqual([x["id"] for x in compact["knowledge"]],
                         [x["id"] for x in full["knowledge"]])
        self.assertEqual([x["title"] for x in compact["knowledge"]].count('同名 "規則"'), 2)
        self.assertTrue(all(x["status"] == "verified" for x in compact["knowledge"]))
        self.assertEqual({x["scope"] for x in compact["knowledge"]}, {"global", "project"})
        self.assertIn("漢字\n改行", compact["markdown"])
        self.assertIn("Untrusted reference data", compact["markdown"])

    def test_json_errors_and_exit_codes_match(self):
        cases = (("project_config_error", lambda: (self.project / ".recallry.toml").write_text("project_id = 7\n")),
                 ("recallry_unavailable", lambda: self.db.path.unlink()),
                 ("schema_invalid", self.drop_required_index))
        for code, change in cases:
            with self.subTest(code=code):
                if code == "project_config_error":
                    change()
                elif code == "recallry_unavailable":
                    (self.project / ".recallry.toml").write_text('project_id = "sample-project"\n')
                    change()
                else:
                    self.db.initialize()
                    change()
                legacy = self.context("json")
                compact = self.context("json-compact")
                self.assertEqual((legacy.returncode, compact.returncode), (2, 2))
                self.assertEqual(compact.stdout, legacy.stdout)
                self.assertEqual(json.loads(compact.stdout)["error"]["code"], code)

    def drop_required_index(self):
        with self.db.connect() as connection:
            connection.execute("DROP INDEX idx_knowledge_category")

    def test_usage_and_metrics_are_format_independent(self):
        item_id = self.add_verified("確認", "安全本文", scope="project", project="sample-project")
        self.paired(metrics=True)
        self.assertEqual(self.db.get(item_id).use_count, 0)
        with sqlite3.connect(self.root / "data" / "metrics.db") as connection:
            events = connection.execute(
                "SELECT event_type,project_id,command,mode,included_count,injected_chars,truncated "
                "FROM metrics_events ORDER BY id"
            ).fetchall()
            ids = connection.execute(
                "SELECT knowledge_id FROM metrics_event_knowledge ORDER BY event_id"
            ).fetchall()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], events[1])
        self.assertEqual(events[0][0], "automatic_context")
        self.assertEqual(ids, [(item_id,), (item_id,)])
        self.paired(automatic=False)
        self.assertEqual(self.db.get(item_id).use_count, 2)


if __name__ == "__main__":
    unittest.main()
