from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from recallry.db import Database
from recallry.project_config import load_project_identity

from test_recallry import VALID_CONFIG


START = b"<!-- RECALLRY:START -->"
END = b"<!-- RECALLRY:END -->"


class ConnectCliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "recallry"
        self.project = base / "project"
        self.root.mkdir()
        self.project.mkdir()
        (self.root / "config.toml").write_text(VALID_CONFIG)
        (self.root / "templates").mkdir()
        repository = Path(__file__).resolve().parents[1]
        for name in ("recallry-claude-router.md", "recallry-codex-router.md"):
            (self.root / "templates" / name).write_bytes(
                (repository / "templates" / name).read_bytes()
            )
        self.source_root = repository / "src"
        self.assertEqual(self.run_cli("init").returncode, 0)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *arguments: str, readonly: bool = False) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.source_root)
        if readonly:
            environment["RECALLRY_READONLY"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "recallry", "--root", str(self.root), *arguments],
            text=True, capture_output=True, env=environment, check=False,
        )

    def connect(self, project_id: str = "example-project", *extra: str):
        return self.run_cli(
            "connect", str(self.project), "--project-id", project_id, *extra,
        )

    def test_fresh_project_creates_config_and_managed_routers(self):
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_project_identity(self.project).project_id, "example-project")
        self.assertEqual((self.project / ".recallry.toml").read_text(),
                         'project_id = "example-project"\n')
        for name, parent in (("CLAUDE.md", "CLAUDE.md"), ("AGENTS.md", "AGENTS.md")):
            content = (self.project / name).read_bytes()
            self.assertEqual(content.count(START), 1)
            self.assertEqual(content.count(END), 1)
            self.assertIn(f"parent `{parent}`".encode(), content)
            self.assertIn(b"--automatic --format json-compact`", content)
        self.assertIn(".recallry.toml: created", result.stdout)
        self.assertIn("CLAUDE.md: created", result.stdout)
        self.assertIn("AGENTS.md: created", result.stdout)
        self.assertIn("Connection check: ok", result.stdout)

    def test_second_connect_is_idempotent(self):
        self.assertEqual(self.connect().returncode, 0)
        paths = [self.project / name for name in (".recallry.toml", "CLAUDE.md", "AGENTS.md")]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])
        self.assertIn(".recallry.toml: unchanged", result.stdout)
        self.assertIn("CLAUDE.md: unchanged", result.stdout)
        self.assertIn("AGENTS.md: unchanged", result.stdout)

    def test_existing_router_content_is_preserved_and_block_not_duplicated(self):
        claude = b"# User Claude rules\r\nKeep exactly this.\r\n"
        agents = b"# User Codex rules\nKeep exactly this.\n"
        (self.project / "CLAUDE.md").write_bytes(claude)
        (self.project / "AGENTS.md").write_bytes(agents)
        self.assertEqual(self.connect().returncode, 0)
        self.assertTrue((self.project / "CLAUDE.md").read_bytes().startswith(claude))
        self.assertTrue((self.project / "AGENTS.md").read_bytes().startswith(agents))
        self.assertEqual(self.connect().returncode, 0)
        self.assertEqual((self.project / "CLAUDE.md").read_bytes().count(START), 1)
        self.assertEqual((self.project / "AGENTS.md").read_bytes().count(START), 1)

    def test_existing_managed_block_updates_only_inside_markers(self):
        outside_before = b"# User rules\n"
        outside_after = b"\n# User tail\n"
        (self.project / "CLAUDE.md").write_bytes(
            outside_before + START + b"\nold managed content\n" + END + outside_after
        )
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        first = (self.project / "CLAUDE.md").read_bytes()
        self.assertTrue(first.startswith(outside_before))
        self.assertTrue(first.endswith(outside_after))
        template = self.root / "templates" / "recallry-claude-router.md"
        template.write_text(template.read_text() + "- Updated template rule.\n")
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        second = (self.project / "CLAUDE.md").read_bytes()
        self.assertTrue(second.startswith(outside_before))
        self.assertTrue(second.endswith(outside_after))
        self.assertIn(b"Updated template rule.", second)
        self.assertEqual(second.count(START), 1)

    def test_existing_home_requires_explicit_template_and_block_upgrade(self):
        old = b"--automatic --format json`"
        new = b"--automatic --format json-compact`"
        names = (("recallry-claude-router.md", "CLAUDE.md"),
                 ("recallry-codex-router.md", "AGENTS.md"))
        for template_name, project_name in names:
            template = self.root / "templates" / template_name
            template.write_bytes(template.read_bytes().replace(new, old) + b"- Local template rule.\n")
            (self.project / project_name).write_bytes(b"# Custom project instructions\n")
        self.assertEqual(self.connect().returncode, 0)
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.connect().returncode, 0)
        for template_name, project_name in names:
            template = self.root / "templates" / template_name
            project_file = self.project / project_name
            self.assertIn(old, template.read_bytes())
            self.assertIn(old, project_file.read_bytes())
            self.assertIn(b"# Custom project instructions\n", project_file.read_bytes())
            # The documented upgrade changes only this flag in each stored
            # template and connected managed block.
            for path in (template, project_file):
                before = path.read_bytes()
                self.assertEqual(before.count(old), 1)
                path.write_bytes(before.replace(old, new))
                self.assertEqual(path.read_bytes(), before.replace(old, new))
            self.assertIn(b"Local template rule.", template.read_bytes())
            self.assertIn(b"# Custom project instructions\n", project_file.read_bytes())
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE.md: unchanged", result.stdout)
        self.assertIn("AGENTS.md: unchanged", result.stdout)

    def test_config_conflict_fails_before_any_write(self):
        config = self.project / ".recallry.toml"
        claude = self.project / "CLAUDE.md"
        config.write_text('project_id = "other-project"\n')
        claude.write_bytes(b"keep")
        result = self.connect("requested-project")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project_id conflict", result.stderr)
        self.assertEqual(config.read_text(), 'project_id = "other-project"\n')
        self.assertEqual(claude.read_bytes(), b"keep")
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_invalid_marker_fails_before_config_or_other_router_write(self):
        claude = self.project / "CLAUDE.md"
        claude.write_bytes(b"before\n" + START + b"\nbroken\n")
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Recallry marker", result.stderr)
        self.assertFalse((self.project / ".recallry.toml").exists())
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertEqual(claude.read_bytes(), b"before\n" + START + b"\nbroken\n")

    def test_invalid_project_ids_are_rejected(self):
        invalid = ("", " leading", "trailing ", "Uppercase", "has/slash", "bad\nvalue", "x" * 129)
        for project_id in invalid:
            with self.subTest(project_id=project_id):
                result = self.connect(project_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.project / ".recallry.toml").exists())

    def test_missing_path_and_file_path_are_rejected(self):
        missing = self.project.parent / "missing"
        result = self.run_cli("connect", str(missing), "--project-id", "missing")
        self.assertNotEqual(result.returncode, 0)
        file_path = self.project.parent / "file.txt"
        file_path.write_text("x")
        result = self.run_cli("connect", str(file_path), "--project-id", "file-project")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a directory", result.stderr)

    def test_project_root_symlink_is_resolved_but_managed_file_symlink_is_rejected(self):
        linked_root = self.project.parent / "project-link"
        linked_root.symlink_to(self.project, target_is_directory=True)
        result = self.run_cli("connect", str(linked_root), "--project-id", "linked-project")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Path: {self.project.resolve()}", result.stdout)

        other_project = self.project.parent / "other-project"
        other_project.mkdir()
        outside = self.project.parent / "outside-claude.md"
        outside.write_text("outside")
        (other_project / "CLAUDE.md").symlink_to(outside)
        result = self.run_cli(
            "connect", str(other_project), "--project-id", "other-project",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to replace symlink", result.stderr)
        self.assertEqual(outside.read_text(), "outside")
        self.assertFalse((other_project / ".recallry.toml").exists())
        self.assertFalse((other_project / "AGENTS.md").exists())

    def test_readonly_mode_rejects_connect(self):
        result = self.run_cli(
            "connect", str(self.project), "--project-id", "readonly-project", readonly=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("write operation unavailable in read-only mode", result.stderr)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_dry_run_reports_changes_without_writing(self):
        result = self.connect("dry-project", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run for: dry-project", result.stdout)
        self.assertIn("Connection check: not run (dry-run)", result.stdout)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_connected_project_resolves_identity_and_automatic_context_is_verified_only(self):
        database = Database(self.root / "data" / "recallry.db")
        for title, scope, project, status in (
            ("Global verified", "global", None, "verified"),
            ("Current verified", "project", "context-project", "verified"),
            ("Other verified", "project", "other-project", "verified"),
            ("Current candidate", "project", "context-project", "candidate"),
        ):
            item = database.add(title=title, content="task context", scope=scope, project=project,
                                category="rule", status="candidate")
            if status == "verified":
                database.transition(item.id, "promote")
        before = {item.id: item.use_count for item in database.list()}
        result = self.connect("context-project")
        self.assertEqual(result.returncode, 0, result.stderr)
        context = self.run_cli(
            "context", "--project-root", str(self.project), "--task", "task context",
            "--automatic", "--format", "json", readonly=True,
        )
        self.assertEqual(context.returncode, 0, context.stderr)
        payload = json.loads(context.stdout)
        self.assertEqual(payload["project_id"], "context-project")
        self.assertEqual({item["title"] for item in payload["knowledge"]},
                         {"Global verified", "Current verified"})
        self.assertTrue(all(item["status"] == "verified" for item in payload["knowledge"]))
        self.assertEqual(before, {item.id: item.use_count for item in database.list()})


if __name__ == "__main__":
    unittest.main()
