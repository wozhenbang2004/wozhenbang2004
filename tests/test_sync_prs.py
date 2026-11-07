import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('sync_prs', Path(__file__).resolve().parents[1] / 'scripts/sync_prs.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def item(number=1, state='open', draft=False, merged=None, title='Example'):
    return {'id': number, 'number': number, 'html_url': f'https://github.com/upstream/project/pull/{number}',
            'title': title, 'state': state, 'draft': draft, 'pull_request': {'merged_at': merged},
            'user': {'login': m.OWNER}, 'updated_at': '2026-11-07T00:00:00Z'}


def page(items, total=None, incomplete=False):
    return {'items': items, 'total_count': len(items) if total is None else total, 'incomplete_results': incomplete}


CURATION = {'adoptions': [], 'notes': {}, 'projects': {}}
TEMPLATE = '\n'.join('outside-' + s + f'\n<!-- {s}_START -->\nold\n<!-- {s}_END -->' for s in ['PR_SUMMARY', 'MERGED_TABLE', 'PR_LIST']) + '\noutside-end\n'


class SyncTests(unittest.TestCase):
    def test_four_mutually_exclusive_states(self):
        rows = [item(), item(2, draft=True), item(3, 'closed', merged='2026-11-06T00:00:00Z'), item(4, 'closed', draft=True)]
        self.assertEqual([m.normalize(p)['state'] for p in rows], list(m.STATES))

    def test_reopen_and_draft_conversion(self):
        p = item(state='closed')
        self.assertEqual(m.normalize(p)['state'], 'Closed')
        p.update(state='open', draft=True)
        self.assertEqual(m.normalize(p)['state'], 'Draft')
        p['draft'] = False
        self.assertEqual(m.normalize(p)['state'], 'Open')

    def test_paginate_beyond_one_hundred(self):
        calls = []
        def get(path):
            calls.append(path)
            return page([item(i) for i in range(1, 101)] if path.endswith('&page=1') else [item(101)], 101)
        self.assertEqual(len(m.fetch_prs(get)), 101)
        self.assertEqual(len(calls), 2)
        self.assertIn('sort=created', calls[0])
        self.assertIn('is%3Apublic', calls[0])
        self.assertIn('-user%3Awozhenbang2004', calls[0])

    def test_refuses_partial_and_truncated_search(self):
        for p in [page([item()], incomplete=True), page([item()], 1001), page([], 0)]:
            with self.subTest(p=p), self.assertRaises(ValueError):
                m.fetch_prs(lambda _: p)

    def test_refuses_duplicate_and_changing_pages(self):
        for last in [page([item(1)], 101), page([item(101)], 102), page([], 101)]:
            replies = iter([page([item(i) for i in range(1, 101)], 101), last])
            with self.subTest(last=last), self.assertRaises(ValueError):
                m.fetch_prs(lambda _: next(replies))

    def test_refuses_unknown_merge_metadata_and_scope(self):
        rows = [item(), item(), item(), item()]
        del rows[0]['pull_request']['merged_at']
        rows[1]['html_url'] = f'https://github.com/{m.OWNER}/fork/pull/1'
        rows[2]['user']['login'] = 'other-user'
        del rows[3]['draft']
        for p in rows:
            with self.subTest(p=p), self.assertRaises(ValueError):
                m.normalize(p)

    def test_adoption_requires_merged_target_and_does_not_change_source_state(self):
        source = m.normalize(item(state='closed'))
        entry = {'source': source['url'], 'target': 'https://github.com/upstream/project/pull/2', 'description': 'Evidence'}
        cur = dict(CURATION, adoptions=[entry])
        for merged, count in [(None, 0), ('2026-11-01T00:00:00Z', 1)]:
            result = m.verified_adoptions([source], cur, lambda _: {'merged_at': merged, 'html_url': entry['target']})
            self.assertEqual(len(result), count)
            self.assertEqual(source['state'], 'Closed')
        source['state'] = 'Merged'
        self.assertEqual(m.verified_adoptions([source], cur, lambda _: self.fail('No lookup needed')), [])

    def test_deduplicates_adoption_targets(self):
        sources = [m.normalize(item(1, 'closed')), m.normalize(item(2, 'closed'))]
        target = 'https://github.com/upstream/project/pull/3'
        cur = dict(CURATION, adoptions=[{'source': p['url'], 'target': target, 'description': ''} for p in sources])
        self.assertEqual(len(m.verified_adoptions(sources, cur, lambda _: {'merged_at': 'date', 'html_url': target})), 1)
        sources.append(m.normalize(item(3, 'closed', merged='date')))
        self.assertEqual(m.verified_adoptions(sources, cur, lambda _: {'merged_at': 'date', 'html_url': target}), [])

    def test_render_preserves_surroundings_and_escapes_titles(self):
        prs = [m.normalize(item(title='<script>alert(1)</script> | [link](url)'))]
        result = m.render(TEMPLATE, prs, [], CURATION, '2026-11-07T00:00:00+00:00')
        self.assertIn('Open 1', result)
        self.assertIn('Draft 0', result)
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertIn('\\|', result)
        self.assertIn('outside-end', result)
        self.assertEqual(result.count('<details>'), result.count('</details>'))

    def test_missing_or_duplicate_markers_fail(self):
        for text in ['', TEMPLATE + '<!-- PR_SUMMARY_START -->']:
            with self.assertRaises(ValueError):
                m.render(text, [m.normalize(item())], [], CURATION, '2026-11-07T00:00:00+00:00')

    def test_sync_is_idempotent_and_failure_preserves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            (root / 'data/pr-curation.json').write_text(json.dumps(CURATION))
            (root / 'README.md').write_text(TEMPLATE)
            m.sync(root, lambda _: page([item()]))
            files = ['README.md', 'data/pull-requests.json']
            before = {p: (root / p).read_bytes() for p in files}
            m.sync(root, lambda _: page([item()]))
            self.assertEqual(before, {p: (root / p).read_bytes() for p in files})
            with self.assertRaises(ValueError):
                m.sync(root, lambda _: page([item()], incomplete=True))
            self.assertEqual(before, {p: (root / p).read_bytes() for p in files})


if __name__ == '__main__':
    unittest.main()
