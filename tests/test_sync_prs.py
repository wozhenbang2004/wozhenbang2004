import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('sync_prs', Path(__file__).resolve().parents[1] / 'scripts/sync_prs.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
DATE = '2026-11-06T00:00:00Z'


def item(number=1, state='closed', merged=DATE, title='Example', draft=False):
    return {'id': number, 'number': number, 'html_url': f'https://github.com/upstream/project/pull/{number}',
            'title': title, 'state': state, 'draft': draft, 'pull_request': {'merged_at': merged},
            'user': {'login': m.OWNER}, 'updated_at': '2026-11-07T00:00:00Z'}


def source_payload(number=10, state='closed', merged=None):
    p = item(number, state, merged)
    p.update(merged_at=merged, base={'repo': {'private': False}})
    return p


def adoption(number=10, target='https://github.com/upstream/project/pull/20'):
    return {'source': f'https://github.com/upstream/project/pull/{number}', 'target': target, 'description': 'Verified source evidence.'}


def page(items, total=None, incomplete=False):
    return {'items': items, 'total_count': len(items) if total is None else total, 'incomplete_results': incomplete}


CURATION = {'adoptions': []}
TEMPLATE = '\n'.join('outside-' + s + f'\n<!-- {s}_START -->\nold\n<!-- {s}_END -->' for s in ['PR_SUMMARY', 'PR_LIST']) + '\noutside-end\n'


class SyncTests(unittest.TestCase):
    def test_merged_query_paginates_beyond_one_hundred(self):
        calls = []
        def get(path):
            calls.append(path)
            return page([item(i) for i in range(1, 101)] if path.endswith('&page=1') else [item(101)], 101)
        self.assertEqual(len(m.fetch_prs(get)), 101)
        self.assertEqual(len(calls), 2)
        for query in ['sort=created', 'is%3Amerged', 'is%3Apublic', '-user%3Awozhenbang2004']:
            self.assertIn(query, calls[0])

    def test_rejects_every_unmerged_state(self):
        for p in [item(state='open', merged=None), item(state='open', merged=None, draft=True), item(merged=None)]:
            with self.subTest(p=p), self.assertRaises(ValueError):
                m.fetch_prs(lambda _: page([p]))
            with self.assertRaises(ValueError):
                m.render(TEMPLATE, [m.normalize(p)], [], CURATION, DATE)

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

    def test_adoption_requires_merged_target_preserves_source_state(self):
        entry = adoption()
        cur = dict(CURATION, adoptions=[entry])
        for state in ('open', 'closed'):
            for merged, count in [(None, 0), (DATE, 1)]:
                def get(path):
                    if path.endswith('/10'):
                        return source_payload(state=state)
                    return {'merged_at': merged, 'html_url': entry['target']}
                result = m.verified_adoptions([], cur, get)
                self.assertEqual(len(result), count)
                if result:
                    self.assertEqual(result[0]['source_state'], state.title())
                    self.assertNotEqual(result[0]['source_state'], 'Merged')

    def test_directly_merged_source_not_double_counted(self):
        cur = dict(CURATION, adoptions=[adoption(1)])
        self.assertEqual(m.verified_adoptions([m.normalize(item())], cur, lambda _: self.fail('No lookup needed')), [])

    def test_private_or_foreign_adoption_source_rejected(self):
        for private, author in [(True, m.OWNER), (False, 'another-user')]:
            raw = source_payload()
            raw['base']['repo']['private'] = private
            raw['user']['login'] = author
            with self.assertRaises(ValueError):
                m.verified_adoptions([], dict(CURATION, adoptions=[adoption()]), lambda _: raw)

    def test_deduplicates_source_and_target_adoptions(self):
        entries = [adoption(), adoption(), adoption(11)]
        def get(path):
            if path.endswith('/10'): return source_payload(10)
            if path.endswith('/11'): return source_payload(11)
            return {'merged_at': DATE, 'html_url': entries[0]['target']}
        self.assertEqual(len(m.verified_adoptions([], dict(CURATION, adoptions=entries), get)), 1)
        self.assertEqual(m.verified_adoptions([m.normalize(item(20))], dict(CURATION, adoptions=entries), get), [])

    def test_commit_adoption_requires_branch_reachability(self):
        sha = 'a' * 40
        entry = dict(adoption(target='https://github.com/upstream/project/commit/' + sha), target_branch='main')
        for status, count in [('ahead', 1), ('identical', 1), ('behind', 0), ('diverged', 0)]:
            def get(path):
                if '/pulls/' in path: return source_payload()
                if '/commits/' in path: return {'sha': sha}
                self.assertIn(sha + '...main', path)
                return {'status': status}
            result = m.verified_adoptions([], dict(CURATION, adoptions=[entry]), get)
            self.assertEqual(len(result), count)
        entry.pop('target_branch')
        with self.assertRaises(ValueError):
            m.verified_adoptions([], dict(CURATION, adoptions=[entry]), lambda _: source_payload())

    def test_render_only_successes_preserves_context_and_evidence(self):
        prs = [m.normalize(item(title='<script>alert(1)</script> | [link](url)'))]
        a = dict(adoption(), source_repo='upstream/project', source_number=10, source_state='Closed', target_kind='pull')
        result = m.render(TEMPLATE, prs, [a], CURATION, DATE)
        self.assertIn('2 项贡献已被上游合入', result)
        self.assertIn('1 条本人 PR 直接合并', result)
        self.assertIn('原 PR：Closed', result)
        self.assertIn(a['source'], result)
        self.assertIn(a['target'], result)
        for absent in ['Open ·', 'Draft ·', 'Closed ·', '| Open |', '全部 PR']:
            self.assertNotIn(absent, result)
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertIn('\\|', result)
        self.assertIn('outside-end', result)
        self.assertEqual(result.count('<details>'), result.count('</details>'))

    def test_missing_or_duplicate_markers_fail(self):
        for text in ['', TEMPLATE + '<!-- PR_SUMMARY_START -->']:
            with self.assertRaises(ValueError):
                m.render(text, [m.normalize(item())], [], CURATION, DATE)

    def test_removed_summary_explanation_stays_absent_on_regeneration(self):
        old_note = '<sub>每小时自动检查；数据变更于 2026-11-07 11:06（北京时间，UTC+8）。仅展示本人向公开外部上游提交且已合并的 PR，以及有明确来源与合入证据的接续采纳。接续采纳保留原 PR 的真实状态，不计作本人 PR 直接合并，同一贡献不重复计数。采纳关系由人工维护证据、自动核对合入状态；已合入开发分支不等于已经发布。GitHub 定时任务可能延迟。</sub>'
        text = TEMPLATE.replace('<!-- PR_SUMMARY_START -->\nold', '<!-- PR_SUMMARY_START -->\n' + old_note)
        prs = [m.normalize(item())]
        for changed_at in (DATE, '2026-11-08T00:00:00Z'):
            text = m.render(text, prs, [], CURATION, changed_at)
            summary = text.split('<!-- PR_SUMMARY_START -->')[1].split('<!-- PR_SUMMARY_END -->')[0]
            self.assertNotIn('<sub>', summary)
            self.assertNotIn('每小时自动检查', text)
            self.assertNotIn('数据变更于', text)
            self.assertIn('1 条本人 PR 直接合并', summary)
            self.assertIn('[同步记录]', summary)

    def test_sync_idempotent_and_failure_preserves_files(self):
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

    def test_removed_manual_highlights_stay_absent_on_regeneration(self):
        old_section = '<details>\n<summary>部分已合入改进说明（人工整理）</summary>\n\n- Old manually written highlight.\n\n</details>'
        text = TEMPLATE.replace('<!-- PR_LIST_START -->\nold', '<!-- PR_LIST_START -->\n' + old_section)
        # Even legacy configuration cannot restore the removed public section.
        cur = dict(CURATION, projects={'upstream/project': {'name': 'Project', 'highlights': 'Old manually written highlight.'}})
        prs = [m.normalize(item())]
        a = dict(adoption(), source_repo='upstream/project', source_number=10, source_state='Closed', target_kind='pull')
        for _ in range(2):
            text = m.render(text, prs, [a], cur, DATE)
            self.assertNotIn('部分已合入改进说明', text)
            self.assertNotIn('Old manually written highlight.', text)
            self.assertIn('查看本人已合并 PR · 1', text)
            self.assertIn('### 经维护者 PR / commit 采纳', text)
            self.assertIn(a['source'], text)
            self.assertIn(a['target'], text)
            self.assertIn('outside-end', text)
            self.assertEqual(text.count('<details>'), 1)
            self.assertEqual(text.count('</details>'), 1)

    def test_removed_project_table_stays_absent_and_pr_list_is_preserved(self):
        prs = [m.normalize(item())]
        a = dict(adoption(), source_repo='upstream/project', source_number=10, source_state='Closed', target_kind='pull')
        text = TEMPLATE
        for _ in range(2):
            text = m.render(text, prs, [a], CURATION, DATE)
            for removed in ['已合入的项目与贡献', 'MERGED_TABLE', '| 项目 |', '| :--- | ---: | ---: |']:
                self.assertNotIn(removed, text)
            self.assertIn('2 项贡献已被上游合入', text)
            self.assertIn('覆盖 1 个公开上游仓库', text)
            self.assertIn('查看本人已合并 PR · 1', text)
            self.assertIn(prs[0]['url'], text)
            self.assertIn('### 经维护者 PR / commit 采纳', text)
            self.assertIn(a['source'], text)
            self.assertIn(a['target'], text)


if __name__ == '__main__':
    unittest.main()
