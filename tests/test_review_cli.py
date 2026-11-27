"""Public consent commands, exercised with a synthetic store and no AI process."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from recallry import cli
from recallry.db import Database
from recallry.review_providers import ClaudeReviewProvider
from recallry.review_settings import CONSENT_VERSION, load_settings, save_settings
from test_recallry import VALID_CONFIG


@unittest.skipUnless(os.name == "posix", "Consent changes use POSIX locks")
class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "store"
        self.root.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(VALID_CONFIG, encoding="utf-8")
        self.db = Database(self.root / "data/recallry.db")
        self.db.initialize()
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "RECALLRY_HOME": str(self.root), "RECALLRY_READONLY": "",
        }))
        self.availability = self.enterContext(mock.patch.object(
            ClaudeReviewProvider, "availability", return_value={"available": True, "message": "Ready"},
        ))
        self.enterContext(mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected subprocess")))

    def invoke(self, action, answer="", *, terminal=False, on_input=None):
        stdin, stdout, stderr = io.StringIO(answer), io.StringIO(), io.StringIO()
        stdin.isatty = stdout.isatty = stderr.isatty = lambda: terminal
        if on_input is not None:
            read = stdin.readline

            def readline(*args):
                on_input(stdout.getvalue() + stderr.getvalue())
                return read(*args)

            stdin.readline = readline
        with mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(["--root", str(self.root), "review", action])
        return status, stdout.getvalue() + stderr.getvalue()

    def store_files(self):
        return {path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_status_shows_default_off_without_writes_or_provider_probes(self):
        original = self.store_files()
        with mock.patch.dict(os.environ, {"RECALLRY_READONLY": "1"}):
            status, output = self.invoke("status")
        self.assertEqual(status, 0, output)
        self.assertRegex(output.upper(), r"\bOFF\b")
        self.assertIn("claude", output.lower())
        self.assertIn("5", output)
        self.assertEqual(self.store_files(), original)
        self.availability.assert_not_called()

    def test_enable_refuses_piped_confirmation(self):
        original = self.config.read_bytes()
        status, output = self.invoke("enable", "ENABLE\n")
        self.assertNotEqual(status, 0, output)
        self.assertEqual(self.config.read_bytes(), original)

    def test_decline_blank_and_eof_do_not_grant_consent(self):
        original = self.config.read_bytes()
        for answer in ("no\n", "\n", ""):
            with self.subTest(answer=answer):
                self.invoke("enable", answer, terminal=True)
                self.assertEqual(self.config.read_bytes(), original)

    def test_explicit_consent_then_revocation_rotates_epoch_without_provider(self):
        def verify_disclosure(display):
            for term in ("claude", "subscription", "human"):
                self.assertIn(term, display.lower())

        original_db = self.db.path.read_bytes()
        code, output = self.invoke("enable", "ENABLE\n", terminal=True, on_input=verify_disclosure)
        self.assertEqual(code, 0, output)
        enabled = load_settings(self.root)
        self.assertTrue(enabled.active)
        self.assertEqual(enabled.consent, CONSENT_VERSION)
        self.availability.assert_called_once_with(check=True)
        self.availability.reset_mock()
        self.assertRegex(self.invoke("status")[1].upper(), r"\bON\b")

        code, output = self.invoke("disable")
        self.assertEqual(code, 0, output)
        disabled = load_settings(self.root)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.consent, "")
        self.assertNotEqual(disabled.epoch, enabled.epoch)
        self.assertEqual((disabled.provider, disabled.threshold), (enabled.provider, enabled.threshold))
        self.assertEqual(self.db.path.read_bytes(), original_db)
        self.availability.assert_not_called()

    def test_unavailable_provider_cannot_be_enabled(self):
        self.availability.return_value = {"available": False, "message": "Unavailable"}
        original = self.config.read_bytes()
        code, output = self.invoke("enable", "ENABLE\n", terminal=True)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.config.read_bytes(), original)

    def test_foreign_store_consent_cannot_be_changed(self):
        save_settings(self.root, enabled=True, threshold=5, provider="claude",
                      consent=True, expected_epoch=load_settings(self.root).epoch)
        with self.db.connect() as connection:
            connection.execute("UPDATE recallry_meta SET value='foreign-app' WHERE key='app_id'")
        original = self.config.read_bytes()
        code, output = self.invoke("disable")
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.config.read_bytes(), original)
        self.availability.assert_not_called()

    def test_nonempty_readonly_blocks_enable_and_disable(self):
        for action, already_enabled in (("enable", False), ("disable", True)):
            with self.subTest(action=action):
                if already_enabled:
                    save_settings(self.root, enabled=True, threshold=7, provider="claude", consent=True,
                                  expected_epoch=load_settings(self.root).epoch)
                original = self.config.read_bytes()
                with mock.patch.dict(os.environ, {"RECALLRY_READONLY": "0"}):
                    code, output = self.invoke(action, "ENABLE\n", terminal=True)
                self.assertNotEqual(code, 0, output)
                self.assertEqual(self.config.read_bytes(), original)
        self.availability.assert_not_called()

    def test_stale_confirmation_cannot_overwrite_another_settings_change(self):
        def change_while_prompt_open(display):
            save_settings(self.root, enabled=False, threshold=9, provider="claude", consent=False,
                          expected_epoch=load_settings(self.root).epoch)

        code, output = self.invoke("enable", "ENABLE\n", terminal=True, on_input=change_while_prompt_open)
        self.assertNotEqual(code, 0, output)
        settings = load_settings(self.root)
        self.assertFalse(settings.active)
        self.assertEqual(settings.threshold, 9)
