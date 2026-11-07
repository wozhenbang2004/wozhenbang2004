#!/usr/bin/env python3
"""Synchronize merged upstream PRs and explicitly evidenced maintainer adoptions."""
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

OWNER = 'wozhenbang2004'
REPO = f'{OWNER}/{OWNER}'
ROOT = Path(__file__).resolve().parents[1]
QUERY = f'is:pr is:merged author:{OWNER} is:public -user:{OWNER}'


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
    rows = [normalize(p) for p in items]
    if any(p['state'] != 'Merged' for p in rows):
        raise ValueError('Merged search returned an unmerged PR; refusing to publish it')
    return sorted(rows, key=lambda p: p['url'])


def verified_adoptions(prs, curation, get=api):
    merged_urls = {p['url'] for p in prs}
    verified, seen_targets, seen_sources = [], set(), set()
    for entry in curation['adoptions']:
        if entry['source'] in merged_urls or entry['source'] in seen_sources:
            continue  # A directly merged source must not be counted a second time.
        source_match = re.fullmatch(r'https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)', entry['source'])
        if not source_match or source_match[1].split('/')[0].lower() == OWNER.lower():
            raise ValueError('Adoption source is outside the upstream scope')
        raw = get(f'/repos/{source_match[1]}/pulls/{source_match[2]}')
        if raw['base']['repo']['private'] is not False or raw['html_url'] != entry['source']:
            raise ValueError('Adoption source is not the expected public PR')
        source = normalize(dict(raw, pull_request={'merged_at': raw['merged_at']}))
        if source['state'] == 'Merged':
            raise ValueError('A curated source merged but is missing from search; retry later')
        target_match = re.fullmatch(r'https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(pull|commit)/([A-Za-z0-9]+)', entry['target'])
        if not target_match or target_match[1].lower() != source['repo'].lower():
            raise ValueError('Adoption must point to the same target upstream repository')
        kind, target_id = target_match[2], target_match[3]
        if kind == 'pull':
            if not target_id.isdigit():
                raise ValueError('Invalid adoption PR number')
            target = get(f'/repos/{target_match[1]}/pulls/{target_id}')
            accepted = bool(target.get('merged_at')) and target['html_url'] == entry['target']
        else:
            # A commit existing in a repository is not enough: it must be on a
            # named upstream branch. Attribution/equivalence remains curated.
            branch = entry.get('target_branch')
            if not re.fullmatch(r'[0-9a-fA-F]{40}', target_id) or not branch:
                raise ValueError('Commit adoption needs a full SHA and target_branch')
            target = get(f'/repos/{target_match[1]}/commits/{target_id}')
            comparison = get(f'/repos/{target_match[1]}/compare/{target_id}...{urllib.parse.quote(branch, safe="")}')
            accepted = (target.get('sha', '').lower() == target_id.lower()
                        and comparison.get('status') in ('ahead', 'identical'))
        if accepted and entry['target'] not in merged_urls and entry['target'] not in seen_targets:
            verified.append(dict(entry, source_repo=source['repo'], source_number=source['number'],
                                 source_state=source['state'], target_kind=kind))
            seen_targets.add(entry['target'])
            seen_sources.add(entry['source'])
    return verified


def escape(value):
    value = html.escape(' '.join(value.split()), quote=False)
    return re.sub(r'([\\`*_{}\[\]()|])', r'\\\1', value)


def fetch_repo_stars(prs, adoptions, get=api):
    repos = {p['repo'] for p in prs} | {a['source_repo'] for a in adoptions}
    stars = {}
    for repo in sorted(repos, key=str.lower):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('Invalid upstream repository name')
        payload = get(f'/repos/{repo}')
        count = payload.get('stargazers_count')
        if (payload.get('private') is not False
                or payload.get('full_name', '').lower() != repo.lower()
                or payload.get('html_url', '').lower() != f'https://github.com/{repo}'.lower()
                or type(count) is not int or count < 0):
            raise ValueError('Missing or inconsistent public repository stars; keeping previous data')
        stars[repo] = count
    return stars


def project_cell(repo, stars):
    owner, name = repo.split('/')
    return (f'**[{escape(name)}](https://github.com/{repo})**<br />'
            f'<sub>{escape(owner)} · [⭐ {stars:,}](https://github.com/{repo}/stargazers)</sub>')


def search_link(extra=''):
    return 'https://github.com/pulls?q=' + urllib.parse.quote(QUERY + (' ' + extra if extra else ''))


def replace_region(text, name, body):
    start, end = f'<!-- {name}_START -->', f'<!-- {name}_END -->'
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
        raise ValueError(f'Missing, duplicate, or reversed {name} markers')
    left, rest = text.split(start)
    _, right = rest.split(end)
    return left + start + '\n' + body.strip() + '\n' + end + right


def render(text, prs, adoptions, repo_stars):
    if any(p['state'] != 'Merged' for p in prs):
        raise ValueError('Only merged PRs belong in the published contribution list')
    all_repos = {p['repo'] for p in prs} | {a['source_repo'] for a in adoptions}
    if (set(repo_stars) != all_repos
            or any(type(value) is not int or value < 0 for value in repo_stars.values())):
        raise ValueError('Every displayed repository needs verified stars')
    summary = (f"**{len(prs) + len(adoptions)} 项贡献已被上游合入** · **{len(prs)} 条本人 PR 直接合并** · **{len(adoptions)} 项经维护者 PR / commit 采纳**  \n"
               f"**覆盖 {len(all_repos)} 个公开上游仓库**\n\n"
               f"[本人 PR 直接合并记录]({search_link()}) · [同步记录](https://github.com/{REPO}/actions/workflows/sync-pr-status.yml)")
    text = replace_region(text, 'PR_SUMMARY', summary)
    body = ['### 已合并 PR', '', '| 项目 / Stars | PR |', '| :--- | :--- |']
    for p in sorted(prs, key=lambda p: (p['merged_at'], p['url']), reverse=True):
        body.append(f"| {project_cell(p['repo'], repo_stars[p['repo']])} | **[#{p['number']}]({p['url']})**<br />{escape(p['title'])} |")
    body += ['', '### 经维护者 PR / commit 采纳', '',
             '| 项目 / Stars | 原 PR → 采纳记录 |', '| :--- | :--- |']
    for a in adoptions:
        target_label = ('PR #' + a['target'].rsplit('/', 1)[1] if a['target_kind'] == 'pull'
                        else 'commit ' + a['target'].rsplit('/', 1)[1][:7])
        description = ' '.join(a['description'].split()).replace('|', r'\|')
        body.append(f"| {project_cell(a['source_repo'], repo_stars[a['source_repo']])} | 原 PR **[#{a['source_number']}]({a['source']})** ({a['source_state']}) → **[{target_label}]({a['target']})**<br />{description} |")
    if not adoptions:
        body.append('| — | 暂无已核实的接续采纳。 |')
    return replace_region(text, 'PR_LIST', '\n'.join(body))


def sync(root=ROOT, get=api):
    curation = json.loads((root / 'data/pr-curation.json').read_text())
    prs = fetch_prs(get)
    adoptions = verified_adoptions(prs, curation, get)
    repo_stars = fetch_repo_stars(prs, adoptions, get)
    snapshot_path = root / 'data/pull-requests.json'
    old = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
    data = {'scope': 'merged-and-evidenced-adoptions', 'pull_requests': prs,
            'verified_adoptions': adoptions, 'repo_stars': repo_stars}
    same = all(old.get(k) == v for k, v in data.items())
    changed_at = old['changed_at'] if same else datetime.now(timezone.utc).isoformat(timespec='seconds')
    snapshot = dict(changed_at=changed_at, **data)
    readme_path = root / 'README.md'
    old_text = readme_path.read_text()
    new_text = render(old_text, prs, adoptions, repo_stars)
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
