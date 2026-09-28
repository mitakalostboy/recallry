from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from recallry.config import resolve_recallry_root
from recallry.context import generate_context
from recallry.db import Database
from recallry.project_config import ProjectConfigError, load_project_identity

from test_recallry import VALID_CONFIG


class ProjectIdentityTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_project_config(self):
        (self.root / ".recallry.toml").write_text('project_id = "example-project"\n')
        identity = load_project_identity(self.root)
        self.assertEqual(identity.project_id, "example-project")
        self.assertEqual(identity.config_path, (self.root / ".recallry.toml").resolve())

    def test_missing_project_config(self):
        with self.assertRaisesRegex(ProjectConfigError, "not found"):
            load_project_identity(self.root)

    def test_malformed_project_config(self):
        (self.root / ".recallry.toml").write_text('project_id = "unterminated\n')
        with self.assertRaisesRegex(ProjectConfigError, "Invalid project config"):
            load_project_identity(self.root)

    def test_missing_empty_and_wrong_type_project_id(self):
        for content in ("name = 'x'\n", "project_id = '   '\n", "project_id = 42\n"):
            with self.subTest(content=content):
                (self.root / ".recallry.toml").write_text(content)
                with self.assertRaisesRegex(ProjectConfigError, "non-empty string"):
                    load_project_identity(self.root)


class AutomaticContextTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "recallry.db")
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def add_verified(self, *, title: str, scope: str = "global", project: str | None = None,
                     content: str = "verified content"):
        item = self.db.add(title=title, content=content, scope=scope, project=project,
                           category="rule", status="candidate")
        return self.db.transition(item.id, "promote")[0]

    def test_verified_only_excludes_candidate(self):
        verified = self.add_verified(title="Verified")
        candidate = self.db.add(title="Candidate", content="candidate", scope="global",
                                project=None, category="rule", status="candidate")
        result = generate_context(self.db, project=None, task="", limit=8,
                                  content_chars=600, max_chars=6000, verified_only=True)
        ids = {entry.item.id for entry in result.included}
        self.assertIn(verified.id, ids)
        self.assertNotIn(candidate.id, ids)

    def test_verified_scope_boundary(self):
        global_item = self.add_verified(title="Global")
        current = self.add_verified(title="Current", scope="project", project="alpha")
        other = self.add_verified(title="Other", scope="project", project="beta")
        result = generate_context(self.db, project="alpha", task="", limit=8,
                                  content_chars=600, max_chars=6000, verified_only=True)
        ids = {entry.item.id for entry in result.included}
        self.assertEqual(ids, {global_item.id, current.id})
        self.assertNotIn(other.id, ids)

    def test_automatic_budget_caps_count_and_chars(self):
        for number in range(12):
            self.add_verified(title=f"Memory {number}", content="x" * 1000)
        result = generate_context(self.db, project=None, task="", limit=8,
                                  content_chars=600, max_chars=6000, verified_only=True)
        self.assertLessEqual(len(result.included), 8)
        self.assertLessEqual(len(result.markdown), 6000)
        self.assertTrue(all(len(entry.content) <= 600 for entry in result.included))


class Phase2CliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "recallry"
        self.project_root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.project_root.mkdir()
        (self.root / "config.toml").write_text(VALID_CONFIG)
        (self.project_root / ".recallry.toml").write_text('project_id = "sample-project"\n')
        self.source_root = Path(__file__).resolve().parents[1] / "src"
        self.assertEqual(self.run_cli("init").returncode, 0)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *arguments: str, readonly: bool = False,
                explicit_root: bool = True,
                environment_updates: dict[str, str] | None = None,
                ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.source_root)
        if readonly:
            environment["RECALLRY_READONLY"] = "1"
        if environment_updates:
            environment.update(environment_updates)
        command = [sys.executable, "-m", "recallry"]
        if explicit_root:
            command.extend(("--root", str(self.root)))
        command.extend(arguments)
        return subprocess.run(command, text=True, capture_output=True,
                              env=environment, check=False)

    def add_verified(self, title: str, *, scope: str = "global", project: str | None = None,
                     content: str = "content"):
        db = Database(self.root / "data" / "recallry.db")
        item = db.add(title=title, content=content, scope=scope, project=project,
                      category="rule", status="candidate")
        db.transition(item.id, "promote")

    def automatic_json(self, *, task: str = "task", readonly: bool = True,
                       extra_arguments: tuple[str, ...] = ()):
        return self.run_cli(
            "context", "--project-root", str(self.project_root), "--task", task,
            "--automatic", "--format", "json", *extra_arguments, readonly=readonly,
        )

    def test_json_output_schema_and_verified_only_automatic_path(self):
        self.add_verified("Global verified", content="task context")
        self.add_verified("Project verified", content="task context", scope="project", project="sample-project")
        self.add_verified("Other verified", scope="project", project="other-project")
        db = Database(self.root / "data" / "recallry.db")
        db.add(title="Candidate", content="candidate", scope="global", project=None,
               category="rule", status="candidate")
        result = self.automatic_json(task="task context")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["project_id"], "sample-project")
        self.assertEqual(payload["task"], "task context")
        self.assertEqual(payload["included_count"], 2)
        self.assertLessEqual(payload["included_chars"], 6000)
        self.assertIsInstance(payload["truncated"], bool)
        self.assertIn("Untrusted reference data", payload["markdown"])
        self.assertEqual({item["title"] for item in payload["knowledge"]},
                         {"Global verified", "Project verified"})
        self.assertTrue(all(item["status"] == "verified" for item in payload["knowledge"]))

    def test_json_zero_result_is_success(self):
        result = self.automatic_json()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["included_count"], 0)
        self.assertEqual(payload["knowledge"], [])

    def test_automatic_cli_enforces_small_budget(self):
        for number in range(12):
            self.add_verified(f"Large {number}", content="task " + "x" * 1000,
                              scope="project", project="sample-project")
        result = self.automatic_json(extra_arguments=("--limit", "999"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["included_count"], 5)
        self.assertLessEqual(payload["included_chars"], 6000)
        self.assertTrue(all(len(item["content"]) <= 600 for item in payload["knowledge"]))

    def test_project_config_error_is_machine_readable(self):
        (self.project_root / ".recallry.toml").write_text("project_id = 7\n")
        result = self.automatic_json()
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "project_config_error")

    def test_uninitialized_recallry_error_is_distinct_from_zero_results(self):
        database = self.root / "data" / "recallry.db"
        database.unlink()
        result = self.automatic_json()
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "recallry_unavailable")

    def test_schema_error_has_distinct_machine_readable_code(self):
        database = self.root / "data" / "recallry.db"
        with Database(database).connect() as connection:
            connection.execute("DROP INDEX idx_knowledge_category")
        result = self.automatic_json()
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "schema_invalid")
        self.assertIn("missing index", payload["error"]["message"])

    def test_automatic_requires_project_root_and_rejects_project_option(self):
        cases = (
            ("context", "--automatic", "--format", "json"),
            ("context", "--project", "sample-project", "--automatic", "--format", "json"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, readonly=True)
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"]["code"], "project_config_error")
                self.assertIn("requires --project-root", payload["error"]["message"])

    def test_automatic_long_task_stays_within_total_budget(self):
        self.add_verified("Relevant", content="content")
        result = self.automatic_json(task="長" * 10_000)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertLessEqual(len(payload["markdown"]), 6000)
        self.assertLessEqual(payload["included_chars"], 6000)

    def test_automatic_does_not_increment_use_count_without_readonly_env(self):
        self.add_verified("No usage mutation")
        db = Database(self.root / "data" / "recallry.db")
        item = db.list()[0]
        result = self.automatic_json(readonly=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(db.get(item.id).use_count, 0)

    def test_readonly_context_and_search_succeed(self):
        self.add_verified("Readable memory")
        db = Database(self.root / "data" / "recallry.db")
        item = db.list()[0]
        context = self.automatic_json()
        search = self.run_cli("search", "Readable", readonly=True)
        self.assertEqual(context.returncode, 0, context.stderr)
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertIn("Readable memory", search.stdout)
        self.assertEqual(db.get(item.id).use_count, 0)

    def test_readonly_rejects_every_write_command(self):
        for arguments in (
            ("init",),
            ("add", "--title", "x", "--content", "x"),
            ("promote", "missing"),
            ("reject", "missing"),
            ("deprecate", "missing"),
        ):
            with self.subTest(command=arguments[0]):
                result = self.run_cli(*arguments, readonly=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("write operation unavailable in read-only mode", result.stderr)

    def test_recallry_home_and_explicit_root_precedence(self):
        environment = {"RECALLRY_HOME": str(self.root)}
        self.assertEqual(resolve_recallry_root(None, environment), self.root.resolve())
        explicit = Path(self.temp.name) / "explicit"
        self.assertEqual(resolve_recallry_root(explicit, environment), explicit.resolve())
        fallback_home = Path(self.temp.name) / "home"
        self.assertEqual(
            resolve_recallry_root(None, {}, home=fallback_home),
            (fallback_home / "Recallry").resolve(),
        )
        self.assertEqual(
            resolve_recallry_root(None, {"RECALLRY_HOME": ""}, home=fallback_home),
            (fallback_home / "Recallry").resolve(),
        )
        process_environment = os.environ.copy()
        process_environment["PYTHONPATH"] = str(self.source_root)
        process_environment["RECALLRY_HOME"] = str(self.root)
        result = subprocess.run(
            [sys.executable, "-m", "recallry", "list"], text=True,
            capture_output=True, env=process_environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_recallry_home_is_unavailable_and_not_created(self):
        missing_root = Path(self.temp.name) / "does-not-exist"
        result = self.run_cli(
            "context", "--project-root", str(self.project_root), "--automatic",
            "--format", "json", readonly=True, explicit_root=False,
            environment_updates={"RECALLRY_HOME": str(missing_root)},
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "recallry_unavailable")
        self.assertFalse(missing_root.exists())

    def test_human_write_operations_still_work(self):
        add = self.run_cli("add", "--title", "Human", "--content", "write")
        self.assertEqual(add.returncode, 0, add.stderr)
        item_id = add.stdout.split()[1]
        promote = self.run_cli("promote", item_id)
        self.assertEqual(promote.returncode, 0, promote.stderr)
        self.assertIn("verified", promote.stdout)


if __name__ == "__main__":
    unittest.main()
