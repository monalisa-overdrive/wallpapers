"""Find images with duplicate filenames across folders.

Reports two kinds of match:
  - Duplicate filenames: the same filename (ignoring case) in more than one folder.
  - Possible duplicates: the same name once processing suffixes (_cropped, -scaled,
    _exported, -removed) and the extension are stripped, e.g. foo_cropped-scaled.png
    and foo_exported.jpg.

Usage:
  python scripts/dup_check.py              # print report
  python scripts/dup_check.py --github     # CI: open/update/close one GitHub issue
"""
import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict

IMAGE_RE = re.compile(r'\.(png|jpe?g|webp)$', re.I)
SUFFIX_RE = re.compile(r'_cropped|_exported|-scaled|-removed', re.I)
ISSUE_TITLE = 'Duplicate filenames'
ISSUE_LABEL = 'duplicate-check'


def find_duplicates(paths):
    by_name, by_stem = defaultdict(list), defaultdict(list)
    for p in paths:
        name = p.rsplit('/', 1)[-1].lower()
        by_name[name].append(p)
        by_stem[SUFFIX_RE.sub('', IMAGE_RE.sub('', name))].append(p)
    exact = [sorted(v) for v in by_name.values() if len({x.rpartition('/')[0] for x in v}) > 1]
    exact_sets = {tuple(g) for g in exact}
    possible = [sorted(v) for v in by_stem.values() if len(v) > 1 and tuple(sorted(v)) not in exact_sets]
    return sorted(exact), sorted(possible)


def report(exact, possible):
    lines = []
    if exact:
        lines += ['### Duplicate filenames', 'The same filename exists in more than one folder:', '']
        for group in exact:
            lines += [f'- {", ".join(f"`{p}`" for p in group)}']
        lines.append('')
    if possible:
        lines += ['### Possible duplicates',
                  'Same name once suffixes like `_cropped`/`-scaled` and the extension are removed:', '']
        for group in possible:
            lines += [f'- {", ".join(f"`{p}`" for p in group)}']
        lines.append('')
    return '\n'.join(lines)


def gh(*args):
    return subprocess.run(['gh', *args], capture_output=True, text=True)


def sync_issue(body):
    """Keep one open issue in sync with the current duplicates."""
    gh('label', 'create', ISSUE_LABEL, '--color', 'FBCA04', '--force',
       '--description', 'Duplicate image filenames found by the duplicate check')
    found = gh('issue', 'list', '--label', ISSUE_LABEL, '--state', 'open',
               '--json', 'number,body', '--jq', '.[0] | "\\(.number)\\n\\(.body)"').stdout
    number, _, old_body = found.partition('\n')
    number = number.strip() if number.strip() not in ('', 'null') else None
    actor = os.environ.get('GITHUB_ACTOR')

    if not body:
        if number:
            gh('issue', 'close', number, '--comment', 'No duplicates left. Closing automatically.')
            print(f'closed issue #{number}')
        return
    full = body + '\nRename or remove files so each image appears once. This issue updates and closes itself.'
    if not number:
        args = ['issue', 'create', '--title', ISSUE_TITLE, '--label', ISSUE_LABEL, '--body', full]
        result = gh(*args, *(['--assignee', actor] if actor else []))
        if result.returncode != 0 and actor:
            result = gh(*args)
        print(result.stdout.strip() or result.stderr.strip())
    elif old_body.strip() != full.strip():
        gh('issue', 'edit', number, '--body', full)
        mention = f' by @{actor}' if actor else ''
        gh('issue', 'comment', number, '--body', f'The duplicate list changed after a push{mention}; see the updated description.')
        print(f'updated issue #{number}')
    else:
        print(f'issue #{number} already up to date')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--github', action='store_true', help='open/update/close a GitHub issue (needs gh)')
    args = ap.parse_args()

    files = subprocess.run(['git', 'ls-files'], capture_output=True, text=True, check=True).stdout.splitlines()
    exact, possible = find_duplicates([f for f in files if IMAGE_RE.search(f)])
    body = report(exact, possible)
    print(body or 'No duplicate filenames.')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('## Duplicate filename check\n\n' + (body or 'No duplicate filenames.') + '\n')
    if args.github:
        sync_issue(body)


if __name__ == '__main__':
    sys.exit(main())
