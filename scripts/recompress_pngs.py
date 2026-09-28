"""Losslessly recompress PNGs (e.g. ones exported with compression level 0).

Each PNG is re-saved at the given zlib level, keeping its color profile, EXIF and
text metadata. The new file only replaces the original if the pixels are
identical and it is at least 5% smaller.

Usage:
  pip install pillow
  python scripts/recompress_pngs.py                  # all PNGs in the repo (tracked + new)
  python scripts/recompress_pngs.py path/to/folder   # just these files/folders
  python scripts/recompress_pngs.py --level 9 ...    # slower, slightly smaller (default 6)
"""
import argparse
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor

from PIL import Image, PngImagePlugin

MIN_SAVING = 0.05


def recompress(path, level):
    before = os.path.getsize(path)
    tmp = path + '.tmp'
    with Image.open(path) as im:
        im.load()
        info = PngImagePlugin.PngInfo()
        for key, value in im.text.items():
            info.add_text(key, value)
        im.save(tmp, 'PNG', compress_level=level, pnginfo=info,
                icc_profile=im.info.get('icc_profile'), exif=im.info.get('exif', b''))
        with Image.open(tmp) as out:
            identical = out.mode == im.mode and out.size == im.size and out.tobytes() == im.tobytes()
    after = os.path.getsize(tmp)
    if not identical:
        os.remove(tmp)
        return path, before, before, 'SKIPPED: pixels differ'
    if after > before * (1 - MIN_SAVING):
        os.remove(tmp)
        return path, before, before, 'already compressed'
    os.replace(tmp, path)
    return path, before, after, 'recompressed'


def find_pngs(paths):
    if not paths:
        out = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
                             capture_output=True, text=True, check=True).stdout.splitlines()
        return [p for p in out if p.lower().endswith('.png') and os.path.isfile(p)]
    found = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                found += [os.path.join(root, n) for n in sorted(names) if n.lower().endswith('.png')]
        elif p.lower().endswith('.png'):
            found.append(p)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*', help='PNG files or folders (default: every PNG in the repo)')
    ap.add_argument('--level', type=int, default=6, choices=range(10), metavar='0-9',
                    help='zlib compression level (default 6)')
    args = ap.parse_args()

    files = find_pngs(args.paths)
    total_before = total_after = 0
    # Large images use ~150 MB each in memory, so keep the pool small.
    with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
        for path, before, after, status in pool.map(recompress, files, [args.level] * len(files)):
            total_before += before
            total_after += after
            print(f'{before / 2**20:7.1f} MB -> {after / 2**20:7.1f} MB  {status:18}  {path}')
    print(f'\nTotal: {total_before / 2**20:.0f} MB -> {total_after / 2**20:.0f} MB')


if __name__ == '__main__':
    main()
