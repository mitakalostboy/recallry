"""Production Fix F2 top-5 contracts; no research imports, model calls, or live databases."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recallry.automatic_context import (
    automatic_candidates, fixf2_spans, generate_automatic_context,
    select_automatic_context,
)
from recallry.cli import main
from recallry.context import generate_context, select_context
from recallry.db import Database
from recallry.search import search


class AutomaticContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'data/recallry.db')
        self.db.initialize()
        (self.root / 'config.toml').write_text('default_context_limit = 15\ndefault_context_content_chars = 800\ndefault_context_max_chars = 12000\ndefault_status = "candidate"\n')
        (self.root / '.recallry.toml').write_text('project_id = "p"\n')
        self.task = ' '.join(f'term{i}' for i in range(25))

    def add(self, content, *, scope='global', project=None, status='verified', title='note'):
        item = self.db.add(title=title, content=content, scope=scope,
                           project=project, category='rule')
        if status in ('verified', 'deprecated'):
            item = self.db.transition(item.id, 'promote')[0]
        if status in ('rejected', 'deprecated'):
            item = self.db.transition(item.id, 'reject' if status == 'rejected' else 'deprecate')[0]
        return item

    def candidates(self, task=None):
        return automatic_candidates(self.db, project='p', task=task or self.task)

    def test_variant_a_collapses_occurrences_and_bigrams(self):
        repeated = self.add(('プロジェクト ' * 30) + '監査', scope='project', project='p')
        broad = self.add('プロジェクト 監査 削除', scope='project', project='p')
        rows = self.candidates('プロジェクト 監査 削除 削除')
        self.assertEqual([(x.item.id, x.span_count, x.rank_score) for x in rows],
                         [(broad.id, 3, 4), (repeated.id, 2, 3)])
        self.assertEqual(len(fixf2_spans('プロジェクト')), 1)
        self.assertEqual(len(fixf2_spans('プロジェクト全体')), 3)
        self.assertEqual(fixf2_spans('これは the and please'), {})

    def test_cjk_boundaries_and_ascii_word_matching(self):
        one = self.add('プロジェクト')
        two = self.add('プロジェクト全体')
        self.assertEqual([x.item.id for x in self.candidates('プロジェクト全体')], [two.id])
        self.assertNotEqual(one.id, two.id)
        self.add('party constitution', scope='project', project='p')
        match = self.add('art', scope='project', project='p')
        self.assertEqual([x.item.id for x in self.candidates('art on')], [match.id])
        for word in ('カタカナ', 'ｶﾀｶﾅ', '漢字', '한국어'):
            with self.subTest(word=word):
                self.assertEqual(len(fixf2_spans(word)), 1)

    def test_project_boost_does_not_change_eligibility_and_matches_frozen_rank(self):
        project = self.add('term0', scope='project', project='p')
        global_item = self.add('term0 term1')
        self.add('unrelated', scope='project', project='p')
        self.add('term0')  # Global eligibility requires two spans.
        with self.db.connect() as connection:
            connection.execute("UPDATE knowledge SET updated_at='2020-01-01'")
        rows = self.candidates()
        self.assertEqual([x.item.id for x in rows], sorted([project.id, global_item.id]))
        by_id = {x.item.id:x for x in rows}
        self.assertEqual((by_id[project.id].span_count, by_id[project.id].rank_score,
                          by_id[project.id].boosted), (1, 2, True))
        self.assertEqual((by_id[global_item.id].rank_score, by_id[global_item.id].boosted), (2, False))
        self.assertEqual([x.id for x in select_automatic_context(self.db, project='p', task=self.task)],
                         [x.item.id for x in rows])

    def test_all_top_five_retained_including_score_two_global_at_rank_five(self):
        expected = []
        for score in (6, 5, 4, 3, 2):
            expected.append(self.add(' '.join(f'term{i}' for i in range(score))).id)
        sixth = self.add('term0', scope='project', project='p')
        with self.db.connect() as connection:
            connection.execute("UPDATE knowledge SET updated_at='2000-01-01' WHERE id=?", (sixth.id,))
        rows = self.candidates()
        self.assertEqual([x.item.id for x in rows], expected)
        self.assertEqual(rows[-1].rank_score, 2)
        selected = select_automatic_context(self.db, project='p', task=self.task)
        self.assertEqual([x.id for x in selected], expected)
        self.assertNotIn(sixth.id, expected)
        self.assertEqual(rows, self.candidates())
        self.assertEqual(selected, select_automatic_context(self.db, project='p', task=self.task))

    def test_fewer_than_five_and_zero_are_not_filled(self):
        project = self.add('term0', scope='project', project='p')
        global_item = self.add('term0 term1')
        self.add('unrelated', scope='project', project='p')
        selected = select_automatic_context(self.db, project='p', task=self.task)
        self.assertEqual({x.id for x in selected}, {project.id, global_item.id})
        result = generate_automatic_context(self.db, project='p', task='unmatched')
        self.assertEqual(result.included, ())
        self.assertIn('No relevant Knowledge found.', result.markdown)

    def test_mixed_ascii_cjk_spans_and_recency_ties(self):
        first = self.add('API プロジェクト監査', scope='project', project='p')
        second = self.add('API プロジェクト監査', scope='project', project='p')
        task = 'API プロジェクト監査 API'
        with self.db.connect() as connection:
            connection.execute("UPDATE knowledge SET updated_at='2020-01-01'")
        rows = self.candidates(task)
        self.assertEqual([x.item.id for x in rows], sorted([first.id, second.id]))
        self.assertTrue(all(x.span_count == 4 and x.rank_score == 5 for x in rows))
        with self.db.connect() as connection:
            connection.execute("UPDATE knowledge SET updated_at='2021-01-01' WHERE id=?", (first.id,))
        self.assertEqual([x.item.id for x in self.candidates(task)], [first.id, second.id])

    def test_verified_only_and_project_boundary(self):
        keep = self.add('term0', scope='project', project='p')
        for status in ('candidate','rejected','deprecated'):
            self.add(self.task, status=status)
            self.add(self.task, scope='project', project='p', status=status)
        self.add(self.task, scope='project', project='other')
        self.assertEqual([x.item.id for x in self.candidates()], [keep.id])

    def test_manual_context_and_search_still_include_unverified_and_fill(self):
        verified = self.add('needle')
        candidate = self.add('needle needle', status='candidate')
        rows = select_context(self.db, project='p', task='unmatched', limit=8)
        self.assertEqual([x.id for x in rows], [verified.id,candidate.id])
        self.assertEqual({x.id for x in search(self.db, 'needle', 8, project='p')},
                         {verified.id,candidate.id})
        result = generate_context(self.db, project='p', task='unmatched', limit=8,
                                  increment_usage=False)
        self.assertEqual({x.item.id for x in result.included}, {verified.id,candidate.id})
        self.assertEqual(select_automatic_context(self.db, project='p', task='unmatched'), [])

    def test_render_budgets_grouping_and_usage_remain_unchanged(self):
        global_item = self.add(self.task + ' x' * 800)
        project = self.add(self.task + ' y' * 800, scope='project', project='p')
        result = generate_automatic_context(self.db, project='p', task=self.task)
        # Selection rank is Project first; legacy Markdown still groups Global first.
        self.assertEqual([x.id for x in select_automatic_context(self.db, project='p', task=self.task)],
                         [project.id,global_item.id])
        self.assertEqual([x.item.id for x in result.included], [global_item.id,project.id])
        self.assertTrue(all(len(x.content)==600 and x.content.endswith('…') for x in result.included))
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.markdown), 6000)
        self.assertEqual(self.db.get(project.id).use_count, 0)
        long_task = self.task + ' unmatched' * 510
        limited = generate_automatic_context(self.db, project='p', task=long_task)
        self.assertLessEqual(len(limited.markdown), 6000)
        self.assertTrue(limited.truncated)
        self.assertTrue(limited.included)
        huge = generate_automatic_context(self.db, project='p', task=self.task * 80)
        self.assertEqual(len(huge.markdown), 6000)
        self.assertEqual(huge.included, ())

    def test_cli_uses_fixed_five_and_compact_contract_without_external_calls(self):
        for i in range(7):
            self.add(self.task + ' x' * 800, title=str(i), scope='project', project='p')
        before = self.db.path.read_bytes()
        for limit in ('1','999'):
            with self.subTest(limit=limit), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'RECALLRY_READONLY':'1','RECALLRY_METRICS':'0'}))
                for target in ('socket.socket','subprocess.Popen','urllib.request.urlopen'):
                    stack.enter_context(patch(target, side_effect=AssertionError('external invocation')))
                output = stack.enter_context(redirect_stdout(StringIO()))
                code = main(['--root',str(self.root),'context','--project-root',str(self.root),
                             '--automatic','--task',self.task,'--limit',limit,'--format','json-compact'])
                self.assertEqual(code,0)
                payload=json.loads(output.getvalue())
                self.assertEqual(payload['included_count'],5)
                self.assertEqual(payload['included_chars'],len(payload['markdown']))
                self.assertLessEqual(payload['included_chars'],6000)
                self.assertTrue(all('content' not in x for x in payload['knowledge']))
                self.assertIn('Reference Knowledge',payload['markdown'])
        self.assertEqual(self.db.path.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
