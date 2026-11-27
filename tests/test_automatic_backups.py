"""Public database fixtures only. No subprocess uses the user's Recallry home."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from recallry.backup import BackupError, STAMP, recover_copy, verify
from recallry.backup_policy import PersonalBackups
from recallry.db import Database
from recallry.metrics import MetricsStore


class BackupTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.area = Path(area.name).resolve()
        self.root = self.area / "store"
        self.db = Database(self.root / "data/recallry.db")
        self.db.initialize()
        self.support = self.area / "state"
        self.policy = PersonalBackups(self.root, support=self.support)

    def add(self, value):
        return self.db.add(title=value, content="A synthetic fixture", scope="global",
                           project=None, category="rule")

    def generations(self):
        return sorted(self.policy.default_base.glob("recallry-backup-*"))

    def test_only_public_store_files_and_recovery_without_consent(self):
        item = self.add("Prime")
        MetricsStore(self.root / "data/metrics.db").append(
            "candidate_created", project_id=None, command="add", mode="manual")
        (self.root / "config.toml").write_text('[automatic_review]\nenabled=true\n')
        (self.root / "data/unrelated.db").write_bytes(b"not a supported store")
        generation = Path(self.policy.take("write")["path"])
        self.assertEqual(set(verify(generation)["files"]), {"recallry.db", "metrics.db"})
        self.assertEqual({p.name for p in generation.iterdir()}, {"recallry.db", "metrics.db", "manifest.json"})
        copy = recover_copy(generation, self.area, self.root)
        self.assertEqual(Database(copy / "data/recallry.db").get(item.id).content, item.content)
        self.assertNotIn("automatic_review", (copy / "config.toml").read_text())
        self.assertEqual((self.root / "config.toml").read_text(), '[automatic_review]\nenabled=true\n')

    def test_defaults_no_reads_write_and_only_due_writes_take_a_snapshot(self):
        self.assertTrue(self.policy.info()["enabled"])
        self.assertFalse(self.support.exists())
        self.add("A")
        self.policy.take("write")
        self.add("B")
        self.assertIsNone(self.policy.take("write"))
        self.assertEqual(len(self.generations()), 1)
        status_path = self.policy.state_dir / "status.json"
        state = json.loads(status_path.read_text())
        state["last_completed"]["at"] = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        status_path.write_text(json.dumps(state))
        self.policy.take("write")
        self.assertEqual(len(self.generations()), 2)

    def test_independent_processes_obey_daily_and_every_write(self):
        code = """
import sys
from pathlib import Path
from recallry.db import Database
from recallry.backup_policy import PersonalBackups
root, support = map(Path, sys.argv[1:3])
Database(root/'data/recallry.db').add(title=sys.argv[3],content='A synthetic fixture',scope='global',
                                     project=None,category='rule')
PersonalBackups(root, support=support).take('write')
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                   PYTHONDONTWRITEBYTECODE="1", HOME=str(self.area))
        for freq, expected in (("daily", 1), ("every", 2)):
            self.policy.configure(enabled=True, frequency=freq, retention=30)
            before = set(self.generations())
            def worker(title):
                return subprocess.run([sys.executable, "-B", "-c", code, str(self.root),
                                       str(self.support), title], env=env, capture_output=True, text=True,
                                      timeout=30)
            with ThreadPoolExecutor() as pool:
                for result in pool.map(worker, (freq + " one", freq + " two")):
                    self.assertEqual(result.returncode, 0, result.stderr)
            generations = set(self.generations()) - before
            self.assertEqual(len(generations), expected)
            for generation in generations:
                verify(generation)
                with closing(sqlite3.connect((generation / "recallry.db").as_uri() + "?mode=ro", uri=True)) as c:
                    self.assertGreater(c.execute("SELECT count(*) FROM knowledge WHERE title LIKE ?",
                                                 (freq + "%",)).fetchone()[0], 0)

    def test_failed_generation_retains_previous_good_and_retries(self):
        self.policy.configure(enabled=True, frequency="every", retention=30)
        self.add("Token")
        old = Path(self.policy.take("write")["path"])
        other = self.root / "data/metrics.db"
        with closing(sqlite3.connect(other)) as c:
            c.execute("CREATE TABLE foreign_data(value)")
        with self.assertRaises(sqlite3.DatabaseError):
            self.policy.take("write")
        self.assertTrue(self.policy.info()["last_failure"])
        self.assertEqual(self.generations(), [old])
        self.assertFalse(list(self.policy.default_base.glob(".partial-*")))
        self.assertIn("recallry.db", verify(old)["files"])
        other.unlink()
        self.policy.take("write")
        self.assertIsNone(self.policy.info()["last_failure"])
        self.assertEqual(len(self.generations()), 2)

    def test_retention_protects_last_verified_fallback_and_unrelated_files(self):
        self.add("Quartz")
        def age(days):
            p = Path(self.policy.take("manual")["path"])
            manifest = json.loads((p / "manifest.json").read_text())
            stamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(STAMP)
            manifest["created_at"] = stamp
            (p / "manifest.json").write_text(json.dumps(manifest))
            dest = p.with_name("recallry-backup-" + stamp + "-" + p.name.rsplit("-", 1)[1])
            p.rename(dest)
            return dest
        self.policy.configure(enabled=True, frequency="every", retention=0)
        old, fallback, corrupt = (age(days) for days in (42, 41, 40))
        (corrupt / "recallry.db").write_bytes(b"damaged fixture")
        unrelated = self.policy.default_base / "recallry-backup-unrelated"
        unrelated.mkdir()
        self.policy.configure(enabled=True, frequency="every", retention=30)
        newest = Path(self.policy.take("write")["path"])
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.is_dir())
        self.assertTrue(corrupt.is_dir())
        verify(fallback)
        verify(newest)
        with self.assertRaises(BackupError):
            recover_copy(corrupt, self.area, self.root)
        self.assertFalse(list(self.area.glob("Recallry-Recovered-*")))


if __name__ == "__main__":
    unittest.main()
