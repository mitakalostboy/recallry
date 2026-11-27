"""Exercise evidence checks with temporary stores and a standalone synthetic Git history."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from recallry import learning
from recallry.db import Database, RecallryError


class LearningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.identity = self.project / ".recallry.toml"
        self.identity.write_text('project_id = "sample"\n')
        self.store = self.base / "store"
        self.db = Database(self.store / "data" / "recallry.db")
        self.db.initialize()
        self.enterContext(patch.dict(os.environ, {"RECALLRY_HOME": str(self.store),
                                                  "RECALLRY_READONLY": "", "RECALLRY_METRICS": "0"}))
        self.git("init", "-q")

    def git(self, *args):
        env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
        env.update(HOME=str(self.base), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "-c", "user.name=Synthetic", "-c", "user.email=test@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false",
                        *args], cwd=self.project, env=env, check=True, capture_output=True)

    def request(self, content="Generate the fixtures before compiling examples.", *, commit=True):
        path = self.project / "convention.md"
        path.write_text(content + "\n")
        if commit:
            self.git("add", "--", path.name)
            self.git("commit", "-qm", "Document a synthetic convention")
        return {"title": "Example fixture convention", "scope": "project", "category": "workflow",
                "content": content, "usefulness": "Apply this convention when building examples.",
                "evidence": [{"kind": "artifact", "path": path.name, "quote": content,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}

    def learn(self, request):
        return learning.learn(self.db, self.project, request)

    def test_committed_exact_quote_promotes_once_under_concurrency(self):
        request = self.request()
        def submit(_):
            return learning.learn(Database(self.db.path), self.project, request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertCountEqual([r["decision"] for r in results], ["promote", "skip"])
        item, = self.db.list()
        self.assertEqual((item.status, item.project), ("verified", "sample"))
        source, events, invalid = learning.learning_history(item.source)
        self.assertFalse(invalid)
        self.assertIn("evidence-gated", source)
        self.assertEqual(events[0]["evidence"][0]["sha256"], request["evidence"][0]["sha256"])
        self.assertIn("commit", events[0]["evidence"][0])

    def test_modified_artifact_stays_pending_and_new_evidence_reuses_same_item(self):
        request = self.request(commit=False)
        first = self.learn(request)
        self.assertEqual((first["decision"], first["reason"]), ("hold", "unverifiable_artifact"))
        self.assertEqual(self.learn(request)["decision"], "skip")
        self.git("add", "convention.md")
        self.git("commit", "-qm", "Commit the previously unattested convention")
        second = self.learn(request)
        self.assertEqual(second["item_id"], first["item_id"])
        self.assertEqual(second["status"], "verified")
        self.assertEqual(len(self.db.list()), 1)

    def test_user_decision_requires_committed_scope_matched_confirmation(self):
        req = self.request()
        req["scope"] = "global"
        self.assertEqual(self.learn(req)["status"], "candidate")
        self.assertEqual(self.db.list(status="verified"), [])
        record = {k: req[k] for k in ("title", "scope", "content", "category")}
        record.update(project=None, confirmed=True, role="user")
        p = self.project / "decisions.json"
        p.write_text(json.dumps([record]))
        self.git("add", p.name)
        self.git("commit", "-qm", "Record a synthetic global decision")
        req["evidence"] = [{"kind": "user_decision", "record": 0, "path": p.name,
                            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}]
        self.assertEqual(self.learn(req)["status"], "verified")

    def test_manual_veto_and_high_impact_claim_never_promote(self):
        request = self.request("Erase the entire database before compiling examples.")
        self.assertEqual(self.learn(request)["reason"], "human_review_high_impact")
        ordinary = self.request("Prepare the reference charts before building documentation.")
        ordinary["title"] = "Reference charts"
        ordinary["review_required"] = True
        self.assertEqual(self.learn(ordinary)["reason"], "human_review_requested")
        ordinary.pop("review_required")
        self.assertEqual(self.learn(ordinary)["status"], "candidate")
        self.assertEqual(self.db.list(status="verified"), [])

    def test_readonly_or_project_veto_leaves_store_untouched(self):
        req = self.request()
        with patch.dict(os.environ, {"RECALLRY_READONLY": "0"}):
            with self.assertRaises(RecallryError):
                self.learn(req)
        self.identity.write_text('project_id = "sample"\nknowledge_writes = false\n')
        with self.assertRaises(RecallryError):
            self.learn(req)
        self.assertFalse(self.db.list())

    def test_schema_identity_and_failed_transaction_preserve_preexisting_rows(self):
        existing = self.db.add(title="Existing", content="Older note", category="rule",
                               scope="project", project="sample")
        req = self.request()
        with patch("recallry.db._TransactionDatabase.transition", side_effect=RecallryError("interrupted")):
            with self.assertRaises(RecallryError):
                self.learn(req)
        self.assertEqual(self.db.list(), [existing])
        with sqlite3.connect(self.db.path) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(dict(con.execute("SELECT key,value FROM recallry_meta")),
                             {"app_id": "recallry", "schema_version": "1"})
