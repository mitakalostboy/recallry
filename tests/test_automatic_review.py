"""End-to-end policy tests, synthetic stores and mock AI only."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import tempfile
import io
import json
import os
from pathlib import Path
import threading
import time
import subprocess
import sys
import unittest
from unittest import mock

from recallry import automatic_review as review, learning, cli
from recallry.db import RecallryError, Database
from recallry.review_settings import load_settings, save_settings
from recallry.review_settings import store_lock
from test_recallry import VALID_CONFIG

CLAIMS = [
    ("Cedar headings", "Cedar headings consistently use sentence case throughout documentation."),
    ("Amber illustrations", "Amber illustrations belong beside their descriptive captions."),
    ("Violet examples", "Violet examples demonstrate each widget using concise snippets."),
    ("Quartz glossary", "Quartz glossary entries contain expanded abbreviations."),
    ("Indigo paragraphs", "Indigo paragraphs separate distinct topics using blank lines."),
    ("Coral diagrams", "Coral diagrams label every arrow with explanatory words."),
]


class ProviderBoundaryTests(unittest.TestCase):
    def test_unvalidated_platform_never_starts_claude(self):
        from recallry.review_providers import ClaudeReviewProvider, ProviderUnavailable
        with mock.patch("sys.platform", "win32"), mock.patch("subprocess.Popen") as spawn:
            provider = ClaudeReviewProvider()
            self.assertFalse(provider.availability(check=True)["available"])
            with self.assertRaises(ProviderUnavailable):
                provider.review({"candidates": []})
            spawn.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX Claude executable profile")
    def test_api_key_or_non_subscription_auth_is_refused(self):
        from recallry.claude_runner import ClaudeCodeRunner, ClaudeUnavailable
        from recallry.review_processes import ChildSupervisor
        with mock.patch("recallry.claude_runner.find_claude", return_value=Path("/unused")):
            runner = ClaudeCodeRunner(ChildSupervisor())
        valid = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
        for changes in ({"apiKeySource": "env"}, {"loggedIn": False},
                        {"authMethod": "apiKey"}, {"apiProvider": "other"}):
            with self.subTest(changes=changes), mock.patch.object(runner, "_auth_raw", return_value=valid | changes):
                with self.assertRaises(ClaudeUnavailable):
                    runner.require_subscription_auth()
        with mock.patch.object(runner, "_auth_raw", return_value=valid):
            self.assertTrue(runner.require_subscription_auth()["loggedIn"])

@unittest.skipUnless(os.name == "posix", "Automatic review requires POSIX advisory locks")
class AutomaticReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="recallry-review-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "synthetic-store"
        self.root.mkdir()
        (self.root / "config.toml").write_text(VALID_CONFIG)
        self.db = Database(self.root / "data/recallry.db")
        self.db.initialize()
        self.project = self.tmp / "synthetic-project"
        self.project.mkdir()
        (self.project / ".recallry.toml").write_text('project_id = "example-project"\n')
        self.blobs = {}
        patcher = mock.patch.dict(os.environ, {"RECALLRY_READONLY": "", "RECALLRY_METRICS": "0",
                                               "RECALLRY_HOME": str(self.root), "HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        git = mock.patch("recallry.learning._git", side_effect=self.git)
        self.git_mock = git.start()
        self.addCleanup(git.stop)

    def git(self, root, *args):
        self.assertEqual(root, self.project)
        if args == ("rev-parse", "--show-toplevel"):
            return os.fsencode(self.project) + b"\n"
        if args == ("rev-parse", "--verify", "HEAD"):
            return b"a" * 40 + b"\n"
        if args[:2] == ("ls-tree", "-z"):
            path = args[-1]
            if path not in self.blobs:
                return b""
            blob = self.blobs[path]
            return b"100644 blob " + hashlib.sha1(blob).hexdigest().encode() + b"\t" + path.encode() + b"\0"
        if args[:1] == ("cat-file",):
            blob = next(value for value in self.blobs.values() if hashlib.sha1(value).hexdigest() == args[2])
            return str(len(blob)).encode() if args[1] == "-s" else blob
        raise AssertionError(f"unexpected Git operation {args}")

    def artifact(self, content, path="docs/conventions.md"):
        file = self.project / path
        file.parent.mkdir(parents=True, exist_ok=True)
        raw = ("# Existing project convention\n\n" + content + "\n").encode()
        file.write_bytes(raw)
        self.blobs[path] = raw
        return {"kind": "artifact", "path": path, "sha256": hashlib.sha256(raw).hexdigest(), "quote": content}

    def request(self, content="Run the formatter before submitting a documentation patch.", **overrides):
        return {"title": "Documentation formatting convention", "content": content,
                "scope": "project", "category": "workflow",
                "usefulness": "Reuse this convention whenever contributors prepare documentation changes.",
                "evidence": [self.artifact(content)], **overrides}

    def learn(self, request):
        return learning.learn(self.db, self.project, request)

    def existing(self, request, status="verified", **overrides):
        values = {key: request[key] for key in ("title", "content", "scope", "category")}
        values.update(project="example-project" if values["scope"] == "project" else None)
        values.update(overrides)
        item = self.db.add(**values)
        if status != "candidate":
            item, _ = self.db.transition(item.id, {"verified": "promote", "rejected": "reject", "deprecated": "deprecate"}[status])
        return item

    def configure(self, **kwargs):
        values = dict(enabled=True, threshold=5, provider="claude", consent=True,
                      expected_epoch=load_settings(self.root).epoch)
        values.update(kwargs)
        return save_settings(self.root, **values)

    def proposal(self, i=0, **kwargs):
        title, content = CLAIMS[i]
        return {"title": title, "content": content, "scope": "project", "category": "workflow",
                "usefulness": "Contributors reuse this convention when writing explanatory material.",
                "evidence": [self.artifact(content, f"docs/example{i}.md")], "review_uncertain": True, **kwargs}

    def provider(self, choice="approve", effect=None):
        provider = mock.Mock()
        provider.review.side_effect = effect or (lambda payload: {"reviews": [
            {"id": item["id"], "decision": choice} for item in payload["candidates"]]})
        patcher = mock.patch.object(review, "get_provider", return_value=provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def test_default_off_and_backlog_never_enrolled(self):
        provider = self.provider()
        request = self.proposal()
        result = learning.learn(self.db, self.project, request)
        old = self.db.get(result["item_id"])
        self.assertFalse(load_settings(self.root).enabled)
        self.configure(threshold=1)
        self.assertEqual(learning.learn(self.db, self.project, request)["reason"], "unchanged_evidence")
        self.assertEqual(review.maybe_review(self.db, self.project)["state"], "below_threshold")
        self.assertEqual(self.db.get(old.id), old)
        provider.review.assert_not_called()

    def test_forced_codex_setting_cannot_spawn_fallback_or_promote(self):
        # Even a synthetic config written below the UI's availability check
        # cannot activate the dormant adapter or send to Claude.
        self.configure(provider="codex", threshold=1)
        from recallry.review_providers import CodexReviewProvider
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no AI process")), \
                mock.patch("recallry.review_providers.ClaudeReviewProvider.review",
                           side_effect=AssertionError("no fallback")), \
                mock.patch.object(CodexReviewProvider, "review", autospec=True,
                                  side_effect=CodexReviewProvider.review) as attempted:
            result = learning.learn(self.db, self.project, self.proposal())
            self.assertEqual(result["status"], "candidate")
            self.assertEqual(result["automatic_review"]["promoted"], [])
            attempted.assert_called_once()
            review.maybe_review(self.db, self.project)
            attempted.assert_called_once()

    def test_five_new_pending_trigger_one_batch_and_preserve_ids_history(self):
        self.configure()
        provider = self.provider()
        before = []
        for i in range(4):
            result = learning.learn(self.db, self.project, self.proposal(i))
            before.append(self.db.get(result["item_id"]))
        provider.review.assert_not_called()
        result = learning.learn(self.db, self.project, self.proposal(4))
        provider.review.assert_called_once()
        self.assertEqual(len(result["automatic_review"]["promoted"]), 5)
        self.assertEqual(len(self.db.list(status="verified")), 5)
        for old in before:
            item = self.db.get(old.id)
            self.assertEqual(item.content, old.content)
            self.assertEqual(item.created_at, old.created_at)
            original, events, invalid = learning.learning_history(item.source)
            self.assertEqual(original, old.source)
            self.assertFalse(invalid)
            self.assertEqual([event["state"] for event in events[1:]], ["started", "finished"])
        review.maybe_review(self.db, self.project)
        provider.review.assert_called_once()

    def test_ai_approval_cannot_replace_independent_evidence(self):
        self.configure(threshold=1)
        provider = self.provider()
        request = self.proposal()
        self.blobs.clear()  # uncommitted bytes
        result = learning.learn(self.db, self.project, request)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["automatic_review"]["promoted"], [])
        provider.review.assert_called_once()
        review.maybe_review(self.db, self.project)
        provider.review.assert_called_once()

    def test_human_vetoes_and_high_impact_are_never_sent(self):
        self.configure(threshold=1)
        provider = self.provider()
        for i, content in enumerate(("Trading orders follow documented financial limits.",
                "Prediction model adoption requires PIT leakage analysis.",
                "Security access must follow the documented approval procedure.",
                "Production recovery follows the recorded backup procedure.")):
            request = self.request(content, title=f"Protected {i}", review_uncertain=True)
            result = learning.learn(self.db, self.project, request)
            self.assertEqual(result["status"], "candidate")
        result = learning.learn(self.db, self.project, self.proposal(review_required=True))
        self.assertEqual(result["status"], "candidate")
        # A later request cannot erase a prior veto, even with a new proof.
        request = self.proposal(review_required=False)
        request["evidence"].append(self.artifact(request["content"], "docs/additional.md"))
        self.assertEqual(learning.learn(self.db, self.project, request)["status"], "candidate")
        provider.review.assert_not_called()

    def test_human_review_from_ai_remains_a_veto_for_v2(self):
        self.configure(threshold=1)
        self.provider("human_review")
        request = self.proposal()
        result = learning.learn(self.db, self.project, request)
        item = self.db.get(result["item_id"])
        self.assertEqual(learning._restriction(item), "human_review_requested")
        request["review_uncertain"] = False
        request["evidence"].append(self.artifact(request["content"], "docs/new.md"))
        self.assertEqual(learning.learn(self.db, self.project, request)["status"], "candidate")

    def test_failure_invalid_output_and_uncertain_crash_are_not_retried(self):
        self.configure(threshold=1)
        provider = self.provider(effect=RuntimeError("secret stderr must not be stored"))
        result = learning.learn(self.db, self.project, self.proposal())
        item = self.db.get(result["item_id"])
        self.assertNotIn("secret stderr", item.source)
        self.assertEqual(result["automatic_review"]["state"], "failed")
        review.maybe_review(self.db, self.project)
        provider.review.assert_called_once()
        provider.review.side_effect = lambda _: {"reviews": [{"id": "invented", "decision": "approve"}]}
        result = learning.learn(self.db, self.project, self.proposal(1))
        self.assertEqual(result["status"], "candidate")
        provider.review.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            learning.learn(self.db, self.project, self.proposal(2))
        calls = provider.review.call_count
        review.maybe_review(self.db, self.project)
        self.assertEqual(provider.review.call_count, calls)

    def test_readonly_and_machine_project_veto_block_every_write(self):
        self.configure(threshold=1)
        provider = self.provider()
        request = self.proposal()
        before = self.db.path.read_bytes()
        for inherited in ("1", "0", "false"):
            with mock.patch.dict(os.environ, {"RECALLRY_READONLY": inherited}):
                with self.assertRaises(RecallryError):
                    learning.learn(self.db, self.project, request)
                with self.assertRaises(RecallryError):
                    self.configure()
                self.assertEqual(review.maybe_review(self.db, self.project)["state"], "read_only")
        (self.project / ".recallry.toml").write_text('project_id = "example-project"\nknowledge_writes = false\n')
        with self.assertRaises(RecallryError):
            learning.learn(self.db, self.project, request)
        self.assertEqual(self.db.path.read_bytes(), before)
        provider.review.assert_not_called()

    def test_settings_validation_persistence_and_consent_epoch(self):
        original = (self.root / "config.toml").read_text()
        for value in (0, -1, True, 1.5, "5"):
            with self.assertRaises(RecallryError):
                self.configure(threshold=value)
        with self.assertRaises(RecallryError):
            self.configure(consent=False)
        with self.assertRaises(RecallryError):
            self.configure(provider="gemini")
        saved = self.configure(threshold=7)
        self.assertEqual(load_settings(self.root), saved)
        self.assertIn(original.strip(), (self.root / "config.toml").read_text())
        self.assertTrue(saved.active)
        self.configure(enabled=False, consent=False)
        self.assertFalse(load_settings(self.root).active)
        with self.assertRaises(RecallryError):
            self.configure(expected_epoch=saved.epoch)

    def test_provider_switch_or_disable_during_review_prevents_promotion(self):
        self.configure(threshold=1)
        def change(payload):
            self.configure(enabled=False, consent=False, provider="codex")
            return {"reviews": [{"id": entry["id"], "decision": "approve"} for entry in payload["candidates"]]}
        self.provider(effect=change)
        result = learning.learn(self.db, self.project, self.proposal())
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["automatic_review"]["state"], "settings_changed")
        self.configure(threshold=1)
        self.assertEqual(review.maybe_review(self.db, self.project)["state"], "below_threshold")

    @unittest.skipUnless(sys.platform == "darwin", "Only the macOS Claude Code profile is enabled")
    def test_off_save_at_each_claude_launch_boundary(self):
        from recallry import claude_runner as claude_cli
        from recallry.review_processes import BoundedRunner, ChildSupervisor

        for i, phase in enumerate(("--version", "auth", "before_spawn", "-p")):
            with self.subTest(phase=phase):
                self.configure(threshold=1)
                folder = self.tmp / phase
                folder.mkdir()
                executable = folder / "mock-claude"
                entered, resume = folder / "entered", folder / "resume"
                executable.write_text(f"""#!{sys.executable}
import json, sys, time
from pathlib import Path
step = sys.argv[1]
if step == {phase!r}:
    Path({str(entered)!r}).touch()
    deadline = time.monotonic() + 8
    while not Path({str(resume)!r}).exists() and time.monotonic() < deadline:
        time.sleep(.01)
if step == '--version':
    print('2.1.303 (Claude Code)')
elif step == 'auth':
    print(json.dumps(dict(loggedIn=True, authMethod='claude.ai', apiProvider='firstParty')))
elif step == '-p':
    candidate = json.load(sys.stdin)['candidates'][0]
    print(json.dumps(dict(structured_output=dict(reviews=[dict(id=candidate['id'], decision='approve')]))))
else:
    sys.exit(2)
""")
                executable.chmod(0o700)
                launch_lock_held = []
                real_spawn, real_run = ChildSupervisor.spawn, BoundedRunner.run

                def spawn(supervisor, argv, **kwargs):
                    if argv[1] == "-p":
                        # An independent settings writer must be excluded at actual creation.
                        try:
                            with store_lock(self.root, ".automatic-review-settings.lock", blocking=False):
                                launch_lock_held.append(False)
                        except BlockingIOError:
                            launch_lock_held.append(True)
                    return real_spawn(supervisor, argv, **kwargs)

                def run(runner, argv, **kwargs):
                    if phase == "before_spawn" and argv[1] == "-p":
                        self.configure(enabled=False, consent=False)
                    return real_run(runner, argv, **kwargs)

                with (mock.patch.object(claude_cli, "find_claude", return_value=executable),
                      mock.patch.object(ChildSupervisor, "spawn", spawn),
                      mock.patch.object(BoundedRunner, "run", run),
                      ThreadPoolExecutor(max_workers=2) as pool):
                    request = self.proposal(i)
                    future = pool.submit(learning.learn, self.db, self.project, request)
                    if phase != "before_spawn":
                        try:
                            deadline = time.monotonic() + 4
                            while not entered.exists() and time.monotonic() < deadline:
                                time.sleep(.01)
                            self.assertTrue(entered.exists(), "mock process did not reach the barrier")
                            save = pool.submit(self.configure, enabled=False, consent=False)
                            self.assertFalse(save.result(timeout=2).active)
                        finally:
                            resume.touch()
                    result = future.result(timeout=5)
                self.assertEqual(launch_lock_held, [True] if phase == "-p" else [])
                self.assertEqual(result["automatic_review"]["state"], "settings_changed")
                self.assertEqual(self.db.get(result["item_id"]).status, "candidate")

    def check_batch_bytes(self, total_bytes, count, expected_count):
        from recallry.claude_runner import encode_payload
        self.configure(threshold=count)
        provider = self.provider("hold")
        entries = [dict(id="0" * 36, title=f"Sample {i}", content="", scope="project", category="workflow")
                   for i in range(count)]
        per_item, extra = divmod(total_bytes - len(encode_payload({"candidates": entries})), count)
        for i, entry in enumerate(entries):
            # Three-byte UTF-8 characters and a JSON-escaped quote in each candidate.
            repeats, tail = divmod(per_item + (i < extra) - 2, 3)
            entry["content"] = chr(0x4E00 + i) * repeats + "x" * tail + '"'
            learning.learn(self.db, self.project, self.request(entry["content"], title=entry["title"],
                                                               review_uncertain=True))
        self.assertEqual(len(encode_payload({"candidates": entries})), total_bytes)
        provider.review.assert_called_once()
        sent = provider.review.call_args.args[0]
        self.assertEqual(len(sent["candidates"]), expected_count)
        size = len(encode_payload(sent))
        self.assertLessEqual(size, 96 * 1024)
        if count == expected_count:
            self.assertEqual(size, total_bytes)
        sent_ids = {entry["id"] for entry in sent["candidates"]}
        for item in self.db.list():
            events = learning.learning_history(item.source)[1]
            self.assertEqual(any(e.get("state") == "started" for e in events), item.id in sent_ids)

    def test_complete_utf8_payload_at_exact_limit(self):
        self.check_batch_bytes(96 * 1024, 20, 20)

    def test_complete_utf8_payload_one_byte_over(self):
        self.check_batch_bytes(96 * 1024 + 1, 20, 19)

    def test_batch_count_limit_independent_of_utf8_size(self):
        self.check_batch_bytes(16 * 1024, 21, 20)

    def test_concurrent_manual_change_and_new_conflict_win_over_ai(self):
        self.configure(threshold=1)
        def change(payload):
            self.db.transition(payload["candidates"][0]["id"], "reject")
            return {"reviews": [{"id": entry["id"], "decision": "approve"} for entry in payload["candidates"]]}
        provider = self.provider(effect=change)
        result = learning.learn(self.db, self.project, self.proposal())
        self.assertEqual(result["status"], "rejected")
        request = self.proposal(1)
        def conflict(payload):
            self.existing({**request, "content": request["content"] + " Extra qualification."})
            return {"reviews": [{"id": entry["id"], "decision": "approve"} for entry in payload["candidates"]]}
        provider.review.side_effect = conflict
        result = learning.learn(self.db, self.project, request)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(learning._restriction(self.db.get(result["item_id"])), "possible_verified_conflict")

    def test_concurrency_and_recursion_have_one_attempt(self):
        self.configure(threshold=1)
        entered, release = threading.Event(), threading.Event()
        def slow(payload):
            self.assertEqual(review.maybe_review(self.db, self.project)["state"], "busy")
            entered.set()
            release.wait(5)
            return {"reviews": [{"id": entry["id"], "decision": "approve"} for entry in payload["candidates"]]}
        provider = self.provider(effect=slow)
        request = self.proposal()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(learning.learn, self.db, self.project, request)
            self.assertTrue(entered.wait(5))
            second = pool.submit(learning.learn, self.db, self.project, request)
            self.assertFalse(second.result(timeout=5)["created"])
            release.set()
            self.assertEqual(first.result(timeout=5)["status"], "verified")
        provider.review.assert_called_once()
        self.assertEqual(len(self.db.list()), 1)

    def test_other_project_and_reenabled_epoch_cannot_drain_queue(self):
        self.configure(threshold=5)
        provider = self.provider()
        result = learning.learn(self.db, self.project, self.proposal())
        old = self.db.get(result["item_id"])
        other = self.tmp / "other"
        other.mkdir()
        (other / ".recallry.toml").write_text('project_id = "other"\n')
        self.assertEqual(review.maybe_review(self.db, other)["state"], "below_threshold")
        self.configure(threshold=1)
        self.assertEqual(review.maybe_review(self.db, self.project)["state"], "below_threshold")
        self.assertEqual(self.db.get(old.id), old)
        provider.review.assert_not_called()

    def test_separate_process_observes_single_flight_lock(self):
        self.configure(threshold=1)
        script = """
from pathlib import Path
import sys
from recallry import automatic_review as review
from recallry.db import Database
def forbidden(_): raise AssertionError('test forbids real AI')
review.get_provider = forbidden
print(review.maybe_review(Database(Path(sys.argv[1])), Path(sys.argv[2]))['state'])
"""
        with store_lock(self.root, ".automatic-review-run.lock"):
            result = subprocess.run([sys.executable, "-c", script, str(self.db.path), str(self.project)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "busy"), result.stderr)

    def test_evidence_change_during_inference_prevents_promotion(self):
        self.configure(threshold=1)
        request = self.proposal()
        def mutate(payload):
            (self.project / request["evidence"][0]["path"]).write_text("different evidence")
            return {"reviews": [{"id": entry["id"], "decision": "approve"} for entry in payload["candidates"]]}
        self.provider(effect=mutate)
        result = learning.learn(self.db, self.project, request)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["automatic_review"]["promoted"], [])

    def test_schema_rejects_missing_extra_duplicate_and_executable_decisions(self):
        bad = [None, {}, {"reviews": []}, {"reviews": [{"id": "a", "decision": "reject"}]},
               {"reviews": [{"id": "a", "decision": "approve", "command": "ignored"}]},
               {"reviews": [{"id": "a", "decision": "approve"}], "extra": True},
               {"reviews": [{"id": "a", "decision": "approve"}, {"id": "a", "decision": "approve"}]}]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(ValueError):
                review._valid_response(response, {"a"})

    def test_cli_trigger_and_metrics_do_not_count_promotion_as_creation(self):
        self.configure(threshold=1)
        observed = []
        def approve(payload):
            paths = sorted(backups.default_base.glob("recallry-backup-*"))
            observed.extend(paths)
            return {"reviews": [{"id": item["id"], "decision": "approve"} for item in payload["candidates"]]}
        self.provider(effect=approve)
        path = self.tmp / "request.json"
        path.write_text(json.dumps(self.proposal()))
        from recallry.backup_policy import PersonalBackups
        backups = PersonalBackups(self.root)
        backups.configure(enabled=True, frequency="every", retention=30)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["--root", str(self.root), "learn", "--project-root", str(self.project), "--input", str(path)])
        self.assertEqual(code, 0)
        promoted = json.loads(out.getvalue())["automatic_review"]["promoted"]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(len(observed), 2)
        self.assertEqual(Database(observed[0] / "recallry.db").get(promoted[0]).status, "candidate")
        item = Database(observed[1] / "recallry.db").get(promoted[0])
        self.assertEqual(learning.learning_history(item.source)[1][-1]["state"], "started")
        generation = Path(backups.info()["last_completed"]["path"])
        self.assertNotIn(generation, observed)
        self.assertEqual(Database(generation / "recallry.db").get(promoted[0]), self.db.get(promoted[0]))
        self.assertFalse((self.root / "data/metrics.db").exists())


if __name__ == "__main__":
    unittest.main()
