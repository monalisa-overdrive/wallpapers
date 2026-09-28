"""Keep a single GitHub issue in sync with a check's current findings (needs gh)."""
import os
import subprocess


def gh(*args):
    return subprocess.run(['gh', *args], capture_output=True, text=True)


def sync_issue(title, label, color, description, body, footer):
    """Open, update or close the one open issue carrying `label`.

    An empty body means the problem is gone: the open issue (if any) is closed.
    Otherwise the issue is created (assigned to the pusher) or, if the findings
    changed, its description is replaced and a comment pings the pusher.
    """
    gh('label', 'create', label, '--color', color, '--force', '--description', description)
    found = gh('issue', 'list', '--label', label, '--state', 'open',
               '--json', 'number,body', '--jq', '.[0] | "\\(.number)\\n\\(.body)"').stdout
    number, _, old_body = found.partition('\n')
    number = number.strip() if number.strip() not in ('', 'null') else None
    actor = os.environ.get('GITHUB_ACTOR')

    if not body:
        if number:
            gh('issue', 'close', number, '--comment', 'Everything is fixed. Closing automatically.')
            print(f'closed issue #{number}')
        return
    full = f'{body}\n{footer} This issue updates and closes itself.'
    if not number:
        args = ['issue', 'create', '--title', title, '--label', label, '--body', full]
        result = gh(*args, *(['--assignee', actor] if actor else []))
        if result.returncode != 0 and actor:  # e.g. actor can't be assigned
            result = gh(*args)
        print(result.stdout.strip() or result.stderr.strip())
    elif old_body.strip() != full.strip():
        gh('issue', 'edit', number, '--body', full)
        mention = f' by @{actor}' if actor else ''
        gh('issue', 'comment', number, '--body', f'The findings changed after a push{mention}; see the updated description.')
        print(f'updated issue #{number}')
    else:
        print(f'issue #{number} already up to date')
