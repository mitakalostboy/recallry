from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recallry.context import _tokenize, select_context
from recallry.db import Database


class TokenizeContractTestCase(unittest.TestCase):
    def test_en_ascii_identity_matches_baseline_split(self):
        task = "Fix the Bug ASAP, please!"
        self.assertEqual(_tokenize(task), [term for term in task.lower().split() if term])

    def test_no_space_ja_bigrams(self):
        self.assertEqual(_tokenize("日本語"), ["日本", "本語"])

    def test_identifier_integrity_preserved_alongside_cjk(self):
        tokens = _tokenize("check src/foo_bar.py for 日本語 bug")
        self.assertIn("src/foo_bar.py", tokens)
        self.assertIn("check", tokens)
        self.assertIn("bug", tokens)

    def test_identifier_edge_punctuation_trimmed(self):
        tokens = _tokenize("(foo.bar) 日本語")
        self.assertIn("foo.bar", tokens)
        self.assertNotIn("(foo.bar)", tokens)

    def test_punctuation_is_a_hard_boundary(self):
        tokens = _tokenize("日本語、テスト")
        self.assertEqual(tokens, ["日本", "本語", "テス", "スト"])

    def test_single_char_cjk_run_preserved(self):
        tokens = _tokenize("A 日 B")
        self.assertIn("日", tokens)

    def test_mixed_kana_kanji_katakana_bigrams_across_scripts(self):
        # 東京 (kanji) + タワー (katakana incl. long vowel mark) form one
        # contiguous CJK run; bigrams slide across the script boundary.
        tokens = _tokenize("東京タワー")
        self.assertEqual(tokens, ["東京", "京タ", "タワ", "ワー"])

    def test_hangul_bigrams(self):
        tokens = _tokenize("안녕하세요")
        self.assertEqual(tokens, ["안녕", "녕하", "하세", "세요"])

    def test_empty_task(self):
        self.assertEqual(_tokenize(""), [])

    def test_token_order_matches_input_position(self):
        """_tokenize emits tokens in the order their runs appear in the
        input -- never sorted, deduped, or reordered -- so downstream
        set()/list consumers get a stable, positional token stream."""
        self.assertEqual(
            _tokenize("bbb aaa 東京タワー 日本語"),
            ["bbb", "aaa", "東京", "京タ", "タワ", "ワー", "日本", "本語"],
        )




class TokenizerRetrievalTestCase(unittest.TestCase):
    """Exercises _tokenize through the real select_context
    paths -- verifies the CJK-light tokenizer actually changes retrieval for
    unspaced Japanese, without touching relevance formulas, tie-breaks, or
    Verified/scope semantics."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "recallry.db")
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def add_verified(self, *, title: str, content: str, scope: str = "global",
                      project: str | None = None, category: str = "rule"):
        item = self.db.add(title=title, content=content, scope=scope, project=project,
                            category=category, status="candidate")
        return self.db.transition(item.id, "promote")[0]

    def test_ja_retrieval_regression_partial_match_via_bigrams(self):
        """A whitespace-only tokenizer treats an unspaced JA task as one giant
        token that can never substring-match shorter Knowledge text; bigrams
        fix that without changing scope/verified semantics."""
        target = self.add_verified(title="Deploy rule", content="本番デプロイ前に必ずバックアップを取得する")
        noise = self.add_verified(title="Unrelated", content="別の内容のメモです")
        results = select_context(self.db, project=None, task="本番デプロイの手順は？", limit=8)
        ids = [item.id for item in results]
        self.assertIn(target.id, ids)
        self.assertLess(ids.index(target.id), ids.index(noise.id) if noise.id in ids else len(ids))

    def test_deterministic_tie_break_by_updated_at_then_id(self):
        """Equal-relevance, equal-verified, equal-updated_at items still
        break ties deterministically by the canonical `updated_at DESC, id`
        order (ascending id for a tie) -- the tokenizer change only swaps
        the term extraction, never the sort key or its ordering semantics.

        Ids and updated_at are fixed and inserted in the REVERSE of the
        expected output order, so a pass here proves the result depends on
        (updated_at, id), not on physical insertion order.
        """
        fixed_timestamp = "2026-01-01T00:00:00+00:00"
        rows = (
            ("zzz-second", "Beta"),
            ("aaa-first", "Alpha"),
        )
        with self.db.connect() as connection:
            for item_id, title in rows:
                connection.execute(
                    """INSERT INTO knowledge
                    (id,scope,project,title,content,status,category,source,
                     created_at,updated_at,verified_at,use_count)
                    VALUES (?,'global',NULL,?,?,'verified','rule',NULL,?,?,?,0)""",
                    (item_id, title, "shared_term_xyz", fixed_timestamp, fixed_timestamp, fixed_timestamp),
                )
        results = select_context(self.db, project=None, task="shared_term_xyz", limit=8)
        ids = [item.id for item in results]
        self.assertEqual(ids, ["aaa-first", "zzz-second"])

    def test_verified_only_scope_and_budget_invariants_hold_with_cjk(self):
        verified = self.add_verified(title="Verified", content="重要な日本語のルール")
        candidate = self.db.add(title="Candidate", content="重要な日本語の候補", scope="global",
                                 project=None, category="rule", status="candidate")
        other_project = self.add_verified(title="Other project", content="重要な日本語",
                                           scope="project", project="beta")
        results = select_context(self.db, project="alpha", task="重要な日本語", limit=8,
                                  verified_only=True)
        ids = {item.id for item in results}
        self.assertIn(verified.id, ids)
        self.assertNotIn(candidate.id, ids)
        self.assertNotIn(other_project.id, ids)
        self.assertLessEqual(len(results), 8)




if __name__ == "__main__":
    unittest.main()
