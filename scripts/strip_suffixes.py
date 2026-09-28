"""One-time rename: strip _cropped, -scaled, _exported and -removed from image names.

  3840x2160/akiba-cops_cropped-scaled.png  ->  3840x2160/akiba-cops.png

Uses `git mv` so history follows each file. Skips any rename whose target already
exists or would clash with another rename.

Usage:
  python scripts/strip_suffixes.py           # dry run: show what would change
  python scripts/strip_suffixes.py --apply   # rename files
"""
import argparse
import os
import re
import subprocess
from collections import Counter

IMAGE_RE = re.compile(r'\.(png|jpe?g|webp)$', re.I)
SUFFIX_RE = re.compile(r'_cropped|_exported|-scaled|-removed', re.I)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='rename files (default is a dry run)')
    args = ap.parse_args()

    files = subprocess.run(['git', 'ls-files'], capture_output=True, text=True, check=True).stdout.splitlines()
    renames = []
    for path in files:
        if not IMAGE_RE.search(path):
            continue
        folder, _, name = path.rpartition('/')
        stem, ext = os.path.splitext(name)
        new_stem = SUFFIX_RE.sub('', stem)
        if new_stem != stem:
            renames.append((path, f'{folder}/{new_stem}{ext}' if folder else f'{new_stem}{ext}'))

    targets = Counter(new.lower() for _, new in renames)
    existing = {f.lower() for f in files}
    todo, skipped = [], []
    for old, new in renames:
        if not os.path.exists(old):
            skipped.append((old, new, 'file is missing on disk'))
        elif targets[new.lower()] > 1:
            skipped.append((old, new, 'another file would get the same name'))
        elif new.lower() in existing or os.path.exists(new):
            skipped.append((old, new, 'target already exists'))
        else:
            todo.append((old, new))

    for old, new in todo:
        print(f'{old}  ->  {new}')
        if args.apply:
            subprocess.run(['git', 'mv', old, new], check=True)
    for old, new, why in skipped:
        print(f'SKIP {old}  ->  {new}  ({why})')

    verb = 'Renamed' if args.apply else 'Would rename'
    print(f'\n{verb} {len(todo)} file(s); skipped {len(skipped)}.')
    if not args.apply and todo:
        print('Run with --apply to rename.')


if __name__ == '__main__':
    main()
