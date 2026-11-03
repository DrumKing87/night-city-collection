import io, json, os, re, sys, urllib.parse, urllib.request
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUTDIR = ROOT / 'assets' / 'cards' / 'retail'
REPORT = ROOT / 'data' / 'artwork-report.json'
OUTDIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = 'Mozilla/5.0 (Night City Collection artwork fetcher)'
TARGETS = [
    {'key': f'retail:{n}', 'no': str(n), 'page': f'https://exburst.dev/cyberpunk/cards/MS01-{n}'}
    for n in range(144, 172)
]

def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        ctype = r.headers.get('content-type', '')
    return (data if binary else data.decode('utf-8', 'ignore')), ctype

def extract_image_url(html, page_url):
    pats = [
        r'<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)',
        r'<meta[^>]+name=["']twitter:image["'][^>]+content=["']([^"']+)',
        r'<img[^>]+src=["']([^"']+render[^"']+\.(?:webp|png|jpe?g)[^"']*)',
        r'<img[^>]+src=["']([^"']+\.(?:webp|png|jpe?g)[^"']*)',
    ]
    for pat in pats:
        m = re.search(pat, html, re.I)
        if m:
            return urllib.parse.urljoin(page_url, m.group(1))
    return None

def save_as_webp(img_bytes, dest):
    im = Image.open(io.BytesIO(img_bytes)).convert('RGBA')
    im.save(dest, 'WEBP', quality=92, method=6)

report = {'generatedAt': None, 'fetched': [], 'failed': []}
from datetime import datetime, timezone
report['generatedAt'] = datetime.now(timezone.utc).isoformat()

ok = 0
for t in TARGETS:
    try:
        html, _ = fetch(t['page'])
        img_url = extract_image_url(html, t['page'])
        if not img_url:
            raise RuntimeError('No image URL found')
        img_bytes, ctype = fetch(img_url, binary=True)
        dest = OUTDIR / f"{t['no']}.webp"
        save_as_webp(img_bytes, dest)
        report['fetched'].append({'key': t['key'], 'page': t['page'], 'image': img_url, 'file': str(dest.relative_to(ROOT))})
        ok += 1
        print(f"Fetched {t['key']} -> {img_url}")
    except Exception as e:
        report['failed'].append({'key': t['key'], 'page': t['page'], 'error': str(e)})
        print(f"FAILED {t['key']}: {e}")
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"Done: {ok}/{len(TARGETS)} artworks mirrored")
if ok == 0:
    sys.exit('No artworks fetched')
