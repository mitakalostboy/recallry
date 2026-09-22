from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from recallry.config import resolve_recallry_root
from recallry.db import Database, RecallryError
from recallry.metrics import MetricsStore


class ProductIsolationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "store"
        self.repository = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        env = os.environ.copy()
        for key in ("RECALLRY_HOME", "RECALLRY_READONLY", "RECALLRY_METRICS"):
            env.pop(key, None)
        env["PYTHONPATH"] = str(self.repository / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-m", "recallry", "--root", str(self.root), *args],
            env=env, text=True, capture_output=True,
        )

    def test_foreign_home_environment_is_ignored(self):
        # Unrelated environment variables must not select the Recallry home.
        self.assertEqual(
            resolve_recallry_root(None, {"FOREIGN_APP_HOME": str(self.base / "foreign")}, home=self.base),
            (self.base / "Recallry").resolve(),
        )

    def test_init_provisions_packaged_defaults_and_preserves_existing_files(self):
        self.assertEqual(self.cli("init").returncode, 0)
        config = self.root / "config.toml"
        config.write_text(config.read_text() + "\n# custom setting retained\n")
        template = self.root / "templates/recallry-codex-router.md"
        template.write_text(template.read_text() + "\nCustom local guidance.\n")
        before = {p: p.read_bytes() for p in (config, template)}
        self.assertEqual(self.cli("init").returncode, 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_database_rejects_foreign_metadata_without_modification(self):
        # Synthetic foreign metadata; no real product data.
        path = self.base / "foreign.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE foreign_meta(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO foreign_meta VALUES ('app_id', 'foreign-app')")
            connection.execute("PRAGMA user_version=1")
        before = path.read_bytes()
        for action in (Database(path).initialize, Database(path).validate):
            with self.assertRaises(RecallryError):
                action()
            self.assertEqual(path.read_bytes(), before)

    def test_init_rejects_foreign_store_path_without_provisioning(self):
        data = self.root / "data"
        data.mkdir(parents=True)
        path = data / "recallry.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE foreign_meta(value TEXT)")
            connection.execute("INSERT INTO foreign_meta VALUES ('foreign-app')")
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        self.assertNotEqual(self.cli("init").returncode, 0)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                  for p in self.root.rglob("*") if p.is_file()})
        self.assertFalse((self.root / "config.toml").exists())

    def test_wrong_app_identity_is_rejected(self):
        path = self.base / "foreign.db"
        db = Database(path)
        db.initialize()
        with db.connect() as connection:
            connection.execute("UPDATE recallry_meta SET value='foreign-app' WHERE key='app_id'")
        before = path.read_bytes()
        for action in (db.initialize, db.validate):
            with self.assertRaises(RecallryError):
                action()
            self.assertEqual(path.read_bytes(), before)

    def test_connect_preserves_foreign_blocks_and_is_idempotent(self):
        self.assertEqual(self.cli("init").returncode, 0)
        project = self.base / "project"
        project.mkdir()
        foreign = b"User notes\n<!-- FOREIGN-APP:START -->\nForeign sentinel\n<!-- FOREIGN-APP:END -->\n"
        for name in ("CLAUDE.md", "AGENTS.md"):
            (project / name).write_bytes(foreign)
        (project / ".foreign-app.toml").write_text('project_id = "foreign-sample"\n')
        args = ("connect", str(project), "--project-id", "example-project")
        self.assertEqual(self.cli(*args).returncode, 0)
        before = {p.name: p.read_bytes() for p in project.iterdir()}
        for name in ("CLAUDE.md", "AGENTS.md"):
            self.assertTrue(before[name].startswith(foreign))
            self.assertEqual(before[name].count(b"<!-- RECALLRY:START -->"), 1)
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in project.iterdir()})

    def test_metrics_rejects_foreign_application_identity(self):
        path = self.base / "metrics.db"
        store = MetricsStore(path)
        store.append("candidate_created", project_id=None, command="add", mode="manual")
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE metrics_meta SET value='foreign-app-metrics' WHERE key='app_id'")
        before = path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            store.append("candidate_created", project_id=None, command="add", mode="manual")
        self.assertEqual(path.read_bytes(), before)

    def test_packaged_resources_match_repository_examples(self):
        resources = self.repository / "src/recallry/resources"
        for name in ("config.toml", "templates/recallry-claude-router.md", "templates/recallry-codex-router.md"):
            self.assertEqual((resources / name).read_bytes(), (self.repository / name).read_bytes())
