#!/usr/bin/env python3
"""Synchronize public upstream PR states; never infer adoption from a closed PR."""
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

OWNER = 'wozhenbang2004'
REPO = f'{OWNER}/{OWNER}'
ROOT = Path(__file__).resolve().parents[1]
QUERY = f'is:pr author:{OWNER} is:public -user:{OWNER}'
STATES = ('Open', 'Draft', 'Merged', 'Closed')
LABELS = {'Open': '🟢 Open · 等待合入', 'Draft': '⚪ Draft · 草稿',
          'Merged': '🟣 Merged · 本人 PR 已合并', 'Closed': '🔴 Closed · 未直接合并'}


def api(path):
    headers = {'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'profile-pr-status-sync'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    request = urllib.request.Request('https://api.github.com' + path, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def normalize(item):
    url = item['html_url']
    match = re.fullmatch(r'https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)', url)
    if (not match or item['user']['login'].lower() != OWNER.lower()
            or match[1].split('/')[0].lower() == OWNER.lower()):
        raise ValueError('Search returned a PR outside the public upstream author scope')
    pr = item['pull_request']
    if 'merged_at' not in pr or not isinstance(item.get('draft'), bool):
        raise ValueError('GitHub omitted merge/draft metadata; refusing to guess')
    if item['state'] not in ('open', 'closed'):
        raise ValueError('Unknown PR state')
    if pr['merged_at'] and item['state'] != 'closed':
        raise ValueError('Inconsistent merged/open state')
    state = ('Merged' if pr['merged_at'] else
             ('Draft' if item['draft'] else 'Open') if item['state'] == 'open' else 'Closed')
    return {'url': url, 'repo': match[1], 'number': int(match[2]), 'title': item['title'],
            'state': state, 'updated_at': item['updated_at'], 'merged_at': pr['merged_at']}


def fetch_prs(get=api):
    items, total = [], None
    for page in range(1, 11):
        # Creation order avoids comment activity moving entries between pages.
        params = urllib.parse.urlencode({'q': QUERY, 'sort': 'created', 'order': 'asc',
                                         'per_page': 100, 'page': page})
        payload = get('/search/issues?' + params)
        if payload.get('incomplete_results') is not False:
            raise ValueError('Incomplete GitHub search; preserving the previous snapshot')
        count = payload['total_count']
        if count > 1000:
            raise ValueError('Search exceeds 1,000 PRs; split the query before syncing')
        if total is not None and total != count:
            raise ValueError('Search changed during pagination; try the next scheduled run')
        total = count
        items.extend(payload['items'])
        if len(items) >= total:
            break
        if not payload['items']:
            raise ValueError('Missing search page')
    if not total or len(items) != total or len({p['id'] for p in items}) != total:
        raise ValueError('Empty, duplicated, or truncated PR results; keeping previous data')
    return sorted((normalize(p) for p in items), key=lambda p: p['url'])


def verified_adoptions(prs, curation, get=api):
    by_url = {p['url']: p for p in prs}
    verified, seen_targets = [], set()
    for entry in curation['adoptions']:
        source = by_url.get(entry['source'])
        if source is None or source['state'] == 'Merged':
            continue  # Avoid duplicating a directly merged source.
        match = re.fullmatch(r'https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)', entry['target'])
        if not match or match[1].lower() != source['repo'].lower():
            raise ValueError('Adoption must point to the same target upstream repository')
        target = get(f'/repos/{match[1]}/pulls/{match[2]}')
        if (target.get('merged_at') and target['html_url'] == entry['target']
                and entry['target'] not in by_url and entry['target'] not in seen_targets):
            verified.append(entry)
            seen_targets.add(entry['target'])
    return verified


def escape(value):
    value = html.escape(' '.join(value.split()), quote=False)
    return re.sub(r'([\\`*_{}\[\]()|])', r'\\\1', value)


def search_link(extra=''):
    return 'https://github.com/pulls?q=' + urllib.parse.quote(QUERY + (' ' + extra if extra else ''))


def replace_region(text, name, body):
    start, end = f'<!-- {name}_START -->', f'<!-- {name}_END -->'
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
        raise ValueError(f'Missing, duplicate, or reversed {name} markers')
    left, rest = text.split(start)
    _, right = rest.split(end)
    return left + start + '\n' + body.strip() + '\n' + end + right


def render(text, prs, adoptions, curation, changed_at):
    counts = Counter(p['state'] for p in prs)
    repos = defaultdict(list)
    for p in prs:
        repos[p['repo']].append(p)
    timestamp = datetime.fromisoformat(changed_at).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    summary = (f"**{len(prs)} 条上游 PR · {len(repos)} 个公开仓库**  \n"
               f"**🟢 Open {counts['Open']} · ⚪ Draft {counts['Draft']} · 🟣 Merged {counts['Merged']} · 🔴 Closed {counts['Closed']}**\n\n"
               f"另有 **{len(adoptions)} 项经其他 PR 采纳**（保留原 PR 的真实状态，不重复计入 Merged）。\n\n"
               f"<sub>每小时自动检查；数据变更于 {timestamp}（北京时间，UTC+8）。只统计本人向公开外部仓库提交的 PR，不含自有仓库或本人 fork 内的合并。Draft 从 Open 单列，Closed 指未直接合并；接续采纳由人工维护证据、自动核对目标 PR 已合并。GitHub 定时任务可能延迟；已合入开发分支不等于已经发布。</sub>\n\n"
               f"[全部 PR]({search_link()}) · [Open]({search_link('is:open draft:false')}) · [Draft]({search_link('is:open draft:true')}) · [Merged]({search_link('is:merged')}) · [Closed]({search_link('is:closed is:unmerged')}) · [同步记录](https://github.com/{REPO}/actions/workflows/sync-pr-status.yml)")
    text = replace_region(text, 'PR_SUMMARY', summary)
    table = ['<details>', f'<summary>按项目查看全部 {len(repos)} 个仓库的 PR 状态</summary>', '',
             '| 项目 | Open | Draft | Merged | Closed |', '| :--- | ---: | ---: | ---: | ---: |']
    for repo, rows in sorted(repos.items(), key=lambda pair: pair[0].lower()):
        c = Counter(p['state'] for p in rows)
        table.append(f'| [{repo}](https://github.com/{repo}) | ' + ' | '.join(str(c[s]) for s in STATES) + ' |')
    table.extend(['', '</details>'])
    text = replace_region(text, 'MERGED_TABLE', '\n'.join(table))
    body = []
    adoption_by_source = {a['source']: a for a in adoptions}
    for state in STATES:
        rows = sorted((p for p in prs if p['state'] == state), key=lambda p: (p['updated_at'], p['url']), reverse=True)
        body += ['<details>', f'<summary>{LABELS[state]} · {len(rows)}</summary>', '',
                 '<sub>按 GitHub 最近更新时间排序。</sub>', '']
        for p in rows:
            body += [f"- **[{p['repo']}#{p['number']}]({p['url']})**<br />{escape(p['title'])}"]
            if p['url'] in adoption_by_source:
                a = adoption_by_source[p['url']]
                body[-1] += f"<br /><sub>另经 [#{a['target'].rsplit('/', 1)[1]}]({a['target']}) 采纳；原 PR 状态仍为 {state}。</sub>"
            if p['url'] in curation['notes']:
                body[-1] += '<br /><sub>' + curation['notes'][p['url']] + '</sub>'
            body.append('')
        if not rows:
            body += ['暂无。', '']
        body += ['</details>', '']
    body += ['<details>', f'<summary>经其他 PR 采纳的贡献与来源 · {len(adoptions)}</summary>', '',
             '<sub>这是独立的贡献证据，不是额外的 PR 状态。只有明确收录且目标 PR 已合并才展示。</sub>', '']
    for a in adoptions:
        source = next(p for p in prs if p['url'] == a['source'])
        body += [f"- **[{source['repo']}#{source['number']}]({a['source']})** → [#{a['target'].rsplit('/',1)[1]}]({a['target']})：{a['description'].strip()}", '']
    body += ['</details>', '', '<details>', '<summary>部分已合入改进说明（人工整理）</summary>', '']
    adopted_repos = {next(p['repo'] for p in prs if p['url'] == a['source']) for a in adoptions}
    for repo, project in curation['projects'].items():
        if any(p['state'] == 'Merged' for p in repos.get(repo, [])) or repo in adopted_repos:
            body.append(f"- [{project['name']}](https://github.com/{repo})：{project['highlights']}")
    body += ['', '</details>']
    return replace_region(text, 'PR_LIST', '\n'.join(body))


def sync(root=ROOT, get=api):
    curation = json.loads((root / 'data/pr-curation.json').read_text())
    prs = fetch_prs(get)
    adoptions = verified_adoptions(prs, curation, get)
    snapshot_path = root / 'data/pull-requests.json'
    old = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
    data = {'pull_requests': prs, 'verified_adoptions': adoptions}
    same = all(old.get(k) == v for k, v in data.items())
    changed_at = old['changed_at'] if same else datetime.now(timezone.utc).isoformat(timespec='seconds')
    snapshot = dict(changed_at=changed_at, **data)
    readme_path = root / 'README.md'
    old_text = readme_path.read_text()
    new_text = render(old_text, prs, adoptions, curation, changed_at)
    # All API responses and marker validation have completed before any write.
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
    readme_path.write_text(new_text)
    print(f'Synchronized {len(prs)} PRs: {dict(Counter(p["state"] for p in prs))}')


if __name__ == '__main__':
    try:
        sync()
    except Exception as exc:
        print(f'PR sync failed; previous published snapshot is unchanged: {exc}', file=sys.stderr)
        sys.exit(1)
