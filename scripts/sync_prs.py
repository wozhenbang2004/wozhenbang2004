#!/usr/bin/env python3
"""Synchronize merged PRs, retained-commit adoptions, and curated adoptions."""
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone

OWNER = 'wozhenbang2004'
REPO = f'{OWNER}/{OWNER}'
ROOT = Path(__file__).resolve().parents[1]
QUERY = f'is:pr is:merged author:{OWNER} is:public -user:{OWNER}'
CLOSED_QUERY = f'is:pr is:closed is:unmerged author:{OWNER} is:public -user:{OWNER}'


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


def search_prs(query, get=api):
    items, total = [], None
    for page in range(1, 11):
        # Creation order avoids comment activity moving entries between pages.
        params = urllib.parse.urlencode({'q': query, 'sort': 'created', 'order': 'asc',
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
    if total is None or len(items) != total or len({p['id'] for p in items}) != total:
        raise ValueError('Empty, duplicated, or truncated PR results; keeping previous data')
    return sorted([normalize(p) for p in items], key=lambda p: p['url'])


def fetch_prs(get=api):
    rows = search_prs(QUERY, get)
    if not rows or any(p['state'] != 'Merged' for p in rows):
        raise ValueError('Merged search returned an unmerged PR; refusing to publish it')
    return rows


def api_list(path, get):
    rows = []
    for page in range(1, 11):
        batch = get(f'{path}?per_page=100&page={page}')
        if not isinstance(batch, list):
            raise ValueError('Expected a complete GitHub list')
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError('GitHub list exceeds pagination limit; keeping previous data')


def discovered_adoptions(prs, curated, get=api):
    """Count exact retained commits, never similarity or a closed PR alone.

    Rewritten/cherry-picked commits still need explicit evidence in curation.
    A merged successor can squash its history, so inspect its PR commit list
    before falling back to ancestry on the original upstream target branch.
    """
    counted_targets = {p['url'] for p in prs} | {a['target'] for a in curated}
    counted_sources = {a['source'] for a in curated}
    found = []
    for source in search_prs(CLOSED_QUERY, get):
        if source['url'] in counted_sources:
            continue
        repo = source['repo']
        raw = get(f'/repos/{repo}/pulls/{source["number"]}')
        if (raw['base']['repo']['private'] is not False
                or raw['html_url'] != source['url']):
            raise ValueError('Unexpected adoption source repository')
        current = normalize(dict(raw, pull_request={'merged_at': raw['merged_at']}))
        if current['state'] != 'Closed':
            raise ValueError('Closed PR changed during discovery; retry later')
        sha, branch = raw['head']['sha'], raw['base']['ref']
        if not re.fullmatch(r'[0-9a-f]{40}', sha) or not branch:
            raise ValueError('Missing source commit or upstream branch')
        associated = api_list(f'/repos/{repo}/commits/{sha}/pulls', get)
        targets = [p for p in associated
                   if p.get('merged_at') and p['base']['repo']['private'] is False
                   and p['base']['repo']['full_name'].lower() == repo.lower()]
        # A replacement authored by this account is already in the direct list.
        if any(p['html_url'] in counted_targets for p in targets):
            continue
        entry = None
        for target in sorted(targets, key=lambda p: (p['merged_at'], p['number'])):
            if target['user']['login'].lower() == OWNER.lower():
                continue
            details = get(f'/repos/{repo}/pulls/{target["number"]}')
            commits = api_list(f'/repos/{repo}/pulls/{target["number"]}/commits', get)
            # GitHub caps this endpoint at 250 commits even with pagination.
            if len(commits) != details['commits']:
                raise ValueError(f'Truncated commit list for {target["html_url"]}; curate this adoption explicitly')
            if not details.get('merged_at') or details['html_url'] != target['html_url']:
                raise ValueError('Successor PR changed during discovery; retry later')
            if any(c['sha'] == sha for c in commits):
                # render() owns table-pipe escaping for every description.
                title = escape(source['title']).replace(r'\|', '|')
                entry = {'source': source['url'], 'target': target['html_url'],
                         'target_kind': 'pull', 'evidence_commit': sha,
                         'description': f'{title}。接续 PR 收录原 head commit [{sha[:7]}](https://github.com/{repo}/commit/{sha})，并已合并。'}
                break
        if entry is None:
            try:
                comparison = get(f'/repos/{repo}/compare/{sha}...{urllib.parse.quote(branch, safe="")}')
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                continue  # A removed branch/commit is not evidence of adoption.
            if (comparison.get('status') not in ('ahead', 'identical')
                    or comparison.get('merge_base_commit', {}).get('sha') != sha):
                continue
            entry = {'source': source['url'], 'target': f'https://github.com/{repo}/commit/{sha}',
                     'target_kind': 'commit', 'target_branch': branch, 'evidence_commit': sha,
                     'description': f'原 PR 的 head commit 已进入上游 `{escape(branch)}` 分支；按采纳贡献单列，不计入本人已合并 PR。'}
        if entry['target'] in counted_targets:
            continue
        found.append(dict(entry, source_repo=repo, source_number=source['number'],
                          source_state='Closed', discovery='retained-commit'))
        counted_sources.add(source['url'])
        counted_targets.add(entry['target'])
    return found


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
             '<sub>原 PR 未直接合并，但有上游采纳证据。自动核对保留原 commit 的接续 PR / 分支；重写或 cherry-pick 的记录另行举证，且不重复计入本人已合并 PR。</sub>', '',
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
    adoptions += discovered_adoptions(prs, adoptions, get)
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
    print(f'Synchronized {len(prs)} merged PRs and {len(adoptions)} verified adoptions')


if __name__ == '__main__':
    try:
        sync()
    except Exception as exc:
        print(f'PR sync failed; previous published snapshot is unchanged: {exc}', file=sys.stderr)
        sys.exit(1)
