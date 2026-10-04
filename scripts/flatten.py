"""Build all/, a flat folder of hard links to every tracked wallpaper.

Handy for pointing a slideshow (e.g. Windows' Personalize > Background > Slideshow) at
one folder instead of each resolution folder. all/ is gitignored; hard links need no
special permissions and take no extra disk space.

git replaces files rather than editing them in place, so a pull or checkout that
touches an image breaks its link. Install the hooks to rebuild all/ automatically
after every merge, checkout and rebase:

Usage:
  python scripts/flatten.py                   # build or refresh all/
  python scripts/flatten.py --install-hooks   # also rebuild after git pull/checkout
"""
import argparse
import os
import subprocess

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
OUT_DIR = 'all'
SKIP_DIRS = {'.thumbs', 'staging', OUT_DIR}
HOOKS = ('post-merge', 'post-checkout', 'post-rewrite')
HOOK_MARKER = '# Installed by scripts/flatten.py'
HOOK_BODY = f'#!/bin/sh\n{HOOK_MARKER}\npython scripts/flatten.py --quiet\n'


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, check=True).stdout.strip()


def wanted_links():
    """Map link name -> source path, skipping (and reporting) name clashes."""
    links, clashes = {}, []
    for path in git('ls-files').splitlines():
        top, _, name = path.partition('/')
        if not name or top in SKIP_DIRS or os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
            continue
        if not os.path.exists(path):
            continue
        name = path.rpartition('/')[2]
        key = name.lower()
        if key in links:
            clashes.append(f'{path} (clashes with {links[key][1]})')
            continue
        links[key] = (name, path)
    return {name: path for name, path in links.values()}, clashes


def build(quiet):
    links, clashes = wanted_links()
    os.makedirs(OUT_DIR, exist_ok=True)
    added = removed = 0

    for name in os.listdir(OUT_DIR):
        dest = os.path.join(OUT_DIR, name)
        src = links.get(name)
        if src is None or not os.path.samefile(dest, src):
            os.remove(dest)
            removed += 1

    for name, src in links.items():
        dest = os.path.join(OUT_DIR, name)
        if not os.path.exists(dest):
            os.link(src, dest)
            added += 1

    for c in clashes:
        print(f'skipped {c}')
    if not quiet or added or removed:
        print(f'{OUT_DIR}/: {len(links)} images ({added} linked, {removed} removed)')


def install_hooks():
    hooks_dir = git('rev-parse', '--git-path', 'hooks')
    os.makedirs(hooks_dir, exist_ok=True)
    for hook in HOOKS:
        path = os.path.join(hooks_dir, hook)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                if HOOK_MARKER not in f.read():
                    print(f'left existing {path} alone; add "python scripts/flatten.py --quiet" to it')
                    continue
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(HOOK_BODY)
        os.chmod(path, 0o755)
        print(f'installed {path}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--install-hooks', action='store_true', help='rebuild all/ after git merge, checkout and rebase')
    ap.add_argument('--quiet', action='store_true', help='only print when something changes')
    args = ap.parse_args()

    os.chdir(git('rev-parse', '--show-toplevel'))
    if args.install_hooks:
        install_hooks()
    build(args.quiet)


if __name__ == '__main__':
    main()
