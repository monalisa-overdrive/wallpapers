"""Check images for signals that they were generated or edited with AI.

Signals checked:
  1. Embedded metadata: C2PA / Content Credentials manifests, IPTC digital source
     type, generator prompt/settings fields, and known AI tool names.
  2. Stable Diffusion invisible watermarks (SD 1.x and SDXL reference encoders).
  3. OpenAI's content provenance API (C2PA + SynthID), if OPENAI_API_KEY is set.

No signal does not mean an image is human-made: re-encoding, cropping and scaling
strip metadata and weaken watermarks.

Usage:
  python scripts/ai_check.py staging                 # local scan of a folder
  python scripts/ai_check.py --files-from list.txt   # newline-separated paths
  python scripts/ai_check.py --no-api staging        # skip the OpenAI API
  python scripts/ai_check.py --open-issues ...       # CI: open GitHub issues
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
import types
import zlib

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}
MEDIA_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
API_URL = 'https://api.openai.com/v1/content_provenance_checks'
API_MAX_BYTES = 50 * 1024 * 1024
ISSUE_LABEL = 'ai-check'

# Only matched against metadata (never pixel data), so false positives stay rare.
GENERATOR_RE = re.compile(
    rb'midjourney|dall[-\xb7 ]?e|stable.?diffusion|comfyui|automatic1111|novelai|'
    rb'adobe firefly|leonardo\.ai|ideogram|openai|chatgpt|made with google ai|'
    rb'dreamstudio|nightcafe|craiyon|flux\.1', re.I)
PNG_GENERATOR_KEYS = {b'parameters', b'prompt', b'workflow', b'sd-metadata', b'invokeai_metadata', b'dream'}
SKIP_PNG_CHUNKS = {b'IDAT', b'IHDR', b'PLTE', b'IEND', b'iCCP', b'pHYs', b'bKGD', b'gAMA', b'cHRM', b'sRGB'}

AI = 'ai'          # strong evidence of AI generation or AI editing
REVIEW = 'review'  # worth a human look, not conclusive


def metadata_signals(data):
    """Return (signals, c2pa_blob) from a file's metadata segments."""
    blobs, c2pa, signals = [], [], []
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        i = 8
        while i + 8 <= len(data):
            n, t = struct.unpack('>I4s', data[i:i + 8])
            body = data[i + 8:i + 8 + n]
            i += 12 + n
            if t in SKIP_PNG_CHUNKS:
                continue
            if t == b'caBX':
                c2pa.append(body)
                continue
            if t in (b'tEXt', b'zTXt', b'iTXt'):
                key, _, rest = body.partition(b'\0')
                if t == b'zTXt':
                    try:
                        rest = zlib.decompress(rest[1:])
                    except zlib.error:
                        pass
                if key in PNG_GENERATOR_KEYS:
                    signals.append((AI, f'PNG text field "{key.decode()}" (written by image generators)'))
                body = key + b' ' + rest
            blobs.append(body)
    elif data[:2] == b'\xff\xd8':
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                i += 2 if marker != 0xFF else 1
                continue
            n = struct.unpack('>H', data[i + 2:i + 4])[0]
            seg = data[i + 4:i + 2 + n]
            if marker == 0xDA:  # start of scan: pixel data follows
                break
            (c2pa if marker == 0xEB else blobs).append(seg)
            i += 2 + n
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        i = 12
        while i + 8 <= len(data):
            t, n = data[i:i + 4], struct.unpack('<I', data[i + 4:i + 8])[0]
            body = data[i + 8:i + 8 + n]
            i += 8 + n + (n & 1)
            if t in (b'EXIF', b'XMP '):
                blobs.append(body)
            elif t == b'C2PA':
                c2pa.append(body)
    else:
        signals.append((REVIEW, 'unrecognised file format'))

    meta = b'\n'.join(blobs)
    if b'compositeWithTrainedAlgorithmicMedia' in meta:
        signals.append((AI, 'IPTC digital source type: edited with AI'))
    elif b'trainedAlgorithmicMedia' in meta:
        signals.append((AI, 'IPTC digital source type: AI-generated'))
    for name in sorted({m.decode('latin1').lower() for m in GENERATOR_RE.findall(meta)}):
        signals.append((AI, f'metadata mentions "{name}"'))
    return signals, b''.join(c2pa)


def c2pa_signals(blob):
    """C2PA manifests are also written by cameras and editors; only some mean AI."""
    if not blob:
        return []
    if b'compositeWithTrainedAlgorithmicMedia' in blob:
        return [(AI, 'Content Credentials say the image was edited with AI')]
    if b'trainedAlgorithmicMedia' in blob:
        return [(AI, 'Content Credentials say the image is AI-generated')]
    names = sorted({m.decode('latin1').lower() for m in GENERATOR_RE.findall(blob)})
    if names:
        return [(AI, f'Content Credentials mention {", ".join(names)}')]
    return [(REVIEW, 'has Content Credentials (C2PA) with no AI marker; inspect at contentcredentials.org/verify')]


_decoder_loaded = None


def watermark_signals(path):
    global _decoder_loaded
    if _decoder_loaded is None:
        try:
            # imwatermark imports torch/onnxruntime for a method we don't use.
            for mod in ('torch', 'onnxruntime', 'onnx'):
                sys.modules.setdefault(mod, types.ModuleType(mod))
            import cv2
            from imwatermark import WatermarkDecoder
            _decoder_loaded = (cv2, WatermarkDecoder)
        except ImportError:
            print('note: invisible-watermark/opencv not installed; skipping watermark check', file=sys.stderr)
            _decoder_loaded = False
    if not _decoder_loaded:
        return []
    cv2, WatermarkDecoder = _decoder_loaded
    img = cv2.imread(path)
    if img is None:
        return []
    signals = []
    if WatermarkDecoder('bytes', 136).decode(img, 'dwtDct') == b'StableDiffusionV1':
        signals.append((AI, 'Stable Diffusion 1.x invisible watermark'))
    sdxl = [int(b) for b in bin(0b101100111110110010010000011110111011000110011110)[2:]]
    if [int(b) for b in WatermarkDecoder('bits', 48).decode(img, 'dwtDct')] == sdxl:
        signals.append((AI, 'Stable Diffusion XL invisible watermark'))
    return signals


def parse_duration(value):
    """Parse OpenAI rate-limit durations like '20ms', '1s', '6m0s' into seconds."""
    if not value:
        return None
    try:
        return float(value)  # retry-after is plain seconds
    except ValueError:
        pass
    parts = re.findall(r'([\d.]+)(ms|h|m|s)', value)
    if not parts:
        return None
    scale = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600}
    return sum(float(n) * scale[unit] for n, unit in parts)


class ProvenanceAPI:
    """OpenAI content provenance checks, paced to stay under the (unpublished) rate limit.

    Requests are spaced at least `interval` seconds apart; every 429 doubles the
    spacing. Waits follow the retry-after / x-ratelimit-reset-requests headers when
    OpenAI sends them. If several files in a row exhaust their retries, the API is
    skipped for the rest of the run rather than burning the job's time limit.
    """
    MAX_ATTEMPTS = 6
    MAX_WAIT = 300
    GIVE_UP_AFTER = 3  # consecutive files that failed every retry

    def __init__(self, key, interval):
        import requests
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {key}'
        self.interval = interval
        self.last_request = 0.0
        self.consecutive_failures = 0
        self.disabled = None

    def _post(self, path, media_type):
        wait = self.last_request + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        with open(path, 'rb') as f:
            return self.session.post(API_URL, files={'file': (os.path.basename(path), f, media_type)}, timeout=120)

    def check(self, path):
        """Return (signals, note, complete)."""
        if self.disabled:
            return [], self.disabled, False
        if os.path.getsize(path) > API_MAX_BYTES:
            return [], 'file over 50 MiB, skipped OpenAI check', True
        media_type = MEDIA_TYPES[os.path.splitext(path)[1].lower()]
        err = None
        for attempt in range(self.MAX_ATTEMPTS):
            backoff = min(self.MAX_WAIT, 5 * 2 ** attempt)
            try:
                r = self._post(path, media_type)
            except Exception as e:  # network errors: retry
                err = str(e)
                time.sleep(backoff)
                continue
            if r.status_code == 429:
                self.interval = min(60, max(1, self.interval) * 2)
                wait = (parse_duration(r.headers.get('retry-after'))
                        or parse_duration(r.headers.get('x-ratelimit-reset-requests')) or backoff)
                err = 'HTTP 429 (rate limited)'
                print(f'           rate limited; waiting {wait:.0f}s, then 1 request per {self.interval:.0f}s',
                      file=sys.stderr)
                time.sleep(min(self.MAX_WAIT, wait))
                continue
            if r.status_code >= 500:
                err = f'HTTP {r.status_code}'
                time.sleep(backoff)
                continue
            if r.status_code in (401, 403, 404):
                self.disabled = f'OpenAI API returned HTTP {r.status_code}; check the key and endpoint access'
                print(f'warning: {self.disabled}', file=sys.stderr)
                return [], self.disabled, False
            self.consecutive_failures = 0
            if r.status_code != 200:
                return [], f'OpenAI API HTTP {r.status_code}: {r.text[:200]}', False
            return self._signals(r.json()), None, True
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.GIVE_UP_AFTER:
            self.disabled = f'OpenAI API skipped after {self.GIVE_UP_AFTER} files in a row failed ({err})'
            print(f'warning: {self.disabled}', file=sys.stderr)
        return [], f'OpenAI API failed after retries ({err})', False

    @staticmethod
    def _signals(body):
        signals = []
        for res in body.get('results', []):
            outcome = res.get('outcome')
            if outcome == 'not_detected':
                continue
            kind = 'SynthID watermark' if res.get('type') == 'synthid' else 'OpenAI Content Credentials'
            details = ', '.join(f'{k}={res[k]}' for k in ('model', 'issuer', 'generated_at') if res.get(k))
            text = f'OpenAI provenance check: {kind} {outcome}' + (f' ({details})' if details else '')
            signals.append((AI if outcome == 'detected' else REVIEW, text))
        return signals


def scan(path, api):
    with open(path, 'rb') as f:
        data = f.read()
    signals, c2pa = metadata_signals(data)
    signals += c2pa_signals(c2pa)
    signals += watermark_signals(path)
    note, complete = None, True
    if api:
        api_signals, note, complete = api.check(path)
        signals += api_signals
    return signals, note, complete


def expand(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in sorted(names):
                    if os.path.splitext(n)[1].lower() in IMAGE_EXTS:
                        yield os.path.join(root, n)
        elif os.path.splitext(p)[1].lower() in IMAGE_EXTS and os.path.isfile(p):
            yield p


def gh(*args):
    return subprocess.run(['gh', *args], capture_output=True, text=True)


def open_issue(path, signals):
    title = f'AI check: {path}'
    existing = gh('issue', 'list', '--label', ISSUE_LABEL, '--state', 'all', '--search', f'"{path}" in:title',
                  '--json', 'title', '--jq', '.[].title')
    if title in existing.stdout.splitlines():
        print(f'issue already exists for {path}')
        return
    server, repo, sha = (os.environ.get(k, '') for k in ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_SHA'))
    strong = any(level == AI for level, _ in signals)
    lines = [
        f'**File:** [`{path}`]({server}/{repo}/blob/{sha}/{path})',
        f'**Commit:** {sha[:7]}',
        '',
        'This image looks **AI-generated or AI-edited**:' if strong else 'This image needs a quick human review:',
        '',
        *[f'- {"🤖" if level == AI else "🔍"} {text}' for level, text in signals],
        '',
        'If this is fine (for example, a real photo with Content Credentials), close this issue. '
        'Otherwise remove or replace the file.',
    ]
    args = ['issue', 'create', '--title', title, '--label', ISSUE_LABEL, '--body', '\n'.join(lines)]
    actor = os.environ.get('GITHUB_ACTOR')
    result = gh(*args, *(['--assignee', actor] if actor else []))
    if result.returncode != 0 and actor:  # e.g. actor can't be assigned
        result = gh(*args)
    print(result.stdout.strip() or result.stderr.strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*', help='image files or folders')
    ap.add_argument('--files-from', help='file with one image path per line')
    ap.add_argument('--no-api', action='store_true', help='skip the OpenAI provenance API')
    ap.add_argument('--open-issues', action='store_true', help='open a GitHub issue per flagged file (needs gh)')
    ap.add_argument('--api-interval', type=float, default=5,
                    help='minimum seconds between OpenAI API requests (default 5; grows after rate limiting)')
    args = ap.parse_args()

    paths = list(args.paths)
    if args.files_from:
        with open(args.files_from, encoding='utf-8') as f:
            paths += [line.strip() for line in f if line.strip()]
    files = list(dict.fromkeys(expand(paths)))
    if not files:
        print('no images to check')
        return

    key = os.environ.get('OPENAI_API_KEY')
    api = ProvenanceAPI(key, args.api_interval) if key and not args.no_api else None
    if not api and not args.no_api:
        print('note: OPENAI_API_KEY not set; skipping OpenAI provenance check', file=sys.stderr)

    if args.open_issues:
        gh('label', 'create', ISSUE_LABEL, '--color', 'D93F0B', '--force',
           '--description', 'Image flagged by the AI-image check')

    flagged, notes, incomplete = [], [], []
    for n, path in enumerate(files, 1):
        signals, note, complete = scan(path, api)
        if not complete:
            incomplete.append(path)
        status = ('AI' if any(l == AI for l, _ in signals) else 'REVIEW' if signals
                  else 'ok' if complete else 'INCOMPLETE')
        print(f'[{n}/{len(files)}] {status:10} {path}')
        for level, text in signals:
            print(f'           - {text}')
        if note:
            print(f'           ! {note}')
            notes.append((path, note))
        if signals:
            flagged.append((path, signals))
            if args.open_issues:
                open_issue(path.replace(os.sep, '/'), signals)

    summary = [f'## AI image check\n', f'Checked {len(files)} image(s); {len(flagged)} flagged.\n']
    if api is None:
        summary.append('> OpenAI provenance check was skipped (no `OPENAI_API_KEY`).\n')
    if incomplete:
        summary.append(f'> **The OpenAI check did not finish for {len(incomplete)} image(s)** (see ⚠️ below). '
                       'Re-run this workflow to retry.\n')
    for path, signals in flagged:
        summary.append(f'- `{path}`: ' + '; '.join(text for _, text in signals))
    for path, note in notes:
        summary.append(f'- ⚠️ `{path}`: {note}')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('\n'.join(summary) + '\n')
    print(f'\n{len(flagged)} of {len(files)} image(s) flagged')
    if incomplete:
        print(f'{len(incomplete)} image(s) did not get the OpenAI check; re-run to retry', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
