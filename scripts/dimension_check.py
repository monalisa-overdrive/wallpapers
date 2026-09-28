"""Check that images match their folder's resolution and are exactly 16:9.

Rules, by top-level folder:
  - WIDTHxHEIGHT (e.g. 3840x2160): every image is exactly that size.
  - higher-than-4k: every image is larger than 3840x2160.
  - Every folder except non-standard/: width:height is exactly 16:9.

Usage:
  python scripts/dimension_check.py              # print report
  python scripts/dimension_check.py --github     # CI: open/update/close one GitHub issue
"""
import argparse
import os
import re
import struct
import subprocess

from issue_sync import sync_issue

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}
EXACT_RE = re.compile(r'(\d+)x(\d+)')
HIGHER_RES_FOLDER, HIGHER_RES_MIN = 'higher-than-4k', (3840, 2160)
NO_RATIO_CHECK = {'non-standard'}
ISSUE_TITLE = 'Image dimension problems'
ISSUE_LABEL = 'dimension-check'


def image_size(path):
    """Return (width, height) from the file header, or None if unreadable."""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return struct.unpack('>II', data[16:24])
    if data[:2] == b'\xff\xd8':
        i = 2
        while i + 9 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                i += 2 if marker != 0xFF else 1
                continue
            # SOF markers (baseline, progressive, ...) carry the frame size.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack('>HH', data[i + 5:i + 9])
                return width, height
            i += 2 + struct.unpack('>H', data[i + 2:i + 4])[0]
        return None
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X':
            return 1 + int.from_bytes(data[24:27], 'little'), 1 + int.from_bytes(data[27:30], 'little')
        if chunk == b'VP8L':
            bits = int.from_bytes(data[21:25], 'little')
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if chunk == b'VP8 ':
            w, h = struct.unpack('<HH', data[26:30])
            return w & 0x3FFF, h & 0x3FFF
    return None


def list_images():
    def git(*args):
        return subprocess.run(['git', 'ls-files', *args], capture_output=True, text=True, check=True).stdout.splitlines()
    deleted = set(git('--deleted'))
    return [f for f in git('--cached', '--others', '--exclude-standard')
            if f not in deleted and '/' in f and os.path.splitext(f)[1].lower() in IMAGE_EXTS]


def check(files):
    min_w, min_h = HIGHER_RES_MIN
    problems = []
    for path in sorted(files):
        folder = path.split('/')[0]
        size = image_size(path)
        if size is None:
            problems.append((path, 'could not read image size'))
            continue
        w, h = size
        m = EXACT_RE.fullmatch(folder)
        if m and (w, h) != tuple(map(int, m.groups())):
            problems.append((path, f'is {w}x{h}, but the folder is {m.group(0)}'))
        elif folder == HIGHER_RES_FOLDER and not (w > min_w and h > min_h):
            problems.append((path, f'is {w}x{h}, not larger than {min_w}x{min_h}'))
        if folder not in NO_RATIO_CHECK and w * 9 != h * 16:
            problems.append((path, f'is {w}x{h}, not exactly 16:9 (ratio {w / h:.4f}; 16:9 is 1.7778)'))
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--github', action='store_true', help='open/update/close a GitHub issue (needs gh)')
    args = ap.parse_args()

    files = list_images()
    problems = check(files)
    body = '\n'.join(f'- `{path}` {why}' for path, why in problems)
    print(body or f'All {len(files)} images have the right dimensions.')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('## Image dimension check\n\n' + (body or f'All {len(files)} images OK.') + '\n')
    if args.github:
        sync_issue(ISSUE_TITLE, ISSUE_LABEL, '5319E7', 'Images with the wrong size or aspect ratio',
                   body and f'These images break the folder size or 16:9 rules:\n\n{body}\n',
                   'Resize or move these images.')


if __name__ == '__main__':
    main()
