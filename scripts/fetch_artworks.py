#!/usr/bin/env python3
"""Night City Collection: validate and locally mirror the images of ALL catalog printings.

Runs in GitHub Actions. Each printing is identified by its catalog's (setId, no, key)
and remains independent of other editions. An existing, valid local image is kept;
missing images are retrieved only from the exact catalogued image URL or an
exact-set/number ExBurst card page. Never substitute a Beta printing for Retail.

The generated manifest is intentionally public; no personal collection data is read.
Dependencies: Pillow. Standard-library HTTP; no API key required.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import html as html_module
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / 'index.html'
ASSETS = ROOT / 'assets' / 'cards'
MANIFEST = ROOT / 'data' / 'artwork-manifest.json'
REPORT = ROOT / 'data' / 'artwork-report.json'
USER_AGENT = 'Mozilla/5.0 (compatible; NightCityCollection-ArtworkMirror/2.0)'
MAX_BYTES = 10_000_000
TIMEOUT = 17
WORKERS = 8
# All external hosts were already present as card image sources in the catalog,
# plus ExBurst, which was confirmed to provide exact retail prints 144-171.
IMAGE_HOSTS = {'cdn.tcg.online', 'cdn.lumatcg.com', 'res.cloudinary.com',
               'cdn.exburst.dev', 'fixer-datapad.com'}
EXBURST_CODE = {
  'wnc-retail': 'MS01', 'wnc-beta': 'BETA',
  'heist-retail': 'SD01', 'heist-beta': 'SD01BETA',
  'embracing-retail': 'SD02', 'embracing-beta': 'SD02BETA',
  'prerelease-beta': 'PRERELEASEBETA', 'prerelease-retail': 'PRERELEASERETAIL',
}
WRITE_LOCK = threading.Lock()


def catalog_cards():
    content = INDEX.read_text(encoding='utf-8')
    match = re.search(r'const DB\s*=\s*(\{.*?\})\s*;\s*const CARDS', content, re.S)
    if not match:
        raise RuntimeError('Kartenliste in index.html nicht erkannt')
    db = json.loads(match.group(1))
    cards = db.get('cards', [])
    if len(cards) != 458 or len({c['key'] for c in cards}) != 458:
        raise RuntimeError('Erwartet: 458 eindeutige Kartendrucke; bitte Katalog prüfen')
    for c in cards:
        if c['setId'] not in {s['id'] for s in db['sets']}:
            raise RuntimeError('Unbekanntes Set: '+str(c['setId']))
    return cards


def key_file(card):
    sid = str(card['setId'])
    number = str(card['no']).lower().replace('β', 'b')
    if not re.fullmatch(r'[a-z0-9-]+', sid) or not re.fullmatch(r'[a-z0-9]+', number):
        raise ValueError('Ungültige Kartenkennung: '+str(card['key']))
    # Preserve already-downloaded canonical retail 144-171 images.
    if sid == 'wnc-retail' and number.isdigit() and 144 <= int(number) <= 171:
        return ASSETS / 'retail' / (number + '.webp')
    return ASSETS / sid / (number + '.webp')


def valid_image(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            w, h = image.size
            if w < 240 or h < 330 or not 0.56 <= w / h <= 0.87:
                raise ValueError(f'Kein Kartenbild ({w}×{h})')
            return image.convert('RGB')
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Ungültiges Bildformat') from exc


def valid_local(path):
    try:
        if not path.is_file() or path.stat().st_size < 4000:
            return False
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return (image.width >= 240 and image.height >= 330
                    and 0.56 <= image.width/image.height <= 0.87)
    except Exception:
        return False


def request(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https':
        raise ValueError('Nur HTTPS-Bildquellen erlaubt')
    if parsed.hostname not in (IMAGE_HOSTS | {'exburst.dev'}):
        raise ValueError('Nicht freigegebener Bildserver')
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT, 'Accept': 'image/avif,image/webp,image/jpeg,image/png,text/html;q=0.9,*/*;q=0.8',
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        length = response.headers.get('Content-Length')
        if length and int(length) > MAX_BYTES:
            raise ValueError('Datei über 10 MB')
        result = response.read(MAX_BYTES+1)
        if len(result) > MAX_BYTES:
            raise ValueError('Datei über 10 MB')
        return result, response.headers.get('Content-Type', '')


def normalize_name(value):
    t = unicodedata.normalize('NFKD', str(value or ''))
    t = ''.join(ch for ch in t if not unicodedata.combining(ch))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', t.lower()).split())


def page_variants(card):
    code = EXBURST_CODE.get(card['setId'])
    if not code:
        return []  # No reliable unambiguous numbering for Box Topper pages.
    no = str(card['no']).replace('β', '').upper()
    if not re.fullmatch(r'[0-9]{3}[AB]?', no):
        return []
    ids = [f'{code}-{no}']
    if card['edition'] == 'beta':
        ids.append(f'{code}-B{no}')
    return ['https://exburst.dev/cyberpunk/cards/'+x for x in ids]


def page_valid(page, card, page_url):
    # Avoid a silent mix-up between editions/print numbers. Check the page's
    # canonical code and card title; never assume a valid response is a match.
    raw = html_module.unescape(page)
    title = re.search(r'<title[^>]*>(.*?)</title>', raw, re.I|re.S)
    title_text = re.sub(r'<[^>]+>', ' ', title.group(1)) if title else ''
    head = re.search(r'<h1[^>]*>(.*?)</h1>', raw, re.I|re.S)
    head_text = re.sub(r'<[^>]+>', ' ', head.group(1)) if head else ''
    name = normalize_name(card['name'])
    normalized_title = normalize_name(title_text+' '+head_text)
    if not name or name not in normalized_title:
        return False
    marker = page_url.rsplit('/', 1)[-1]
    if marker not in raw and marker.lower() not in raw.lower():
        return False
    # Beta vs retail must be demonstrable in the card text when path is ambiguous.
    if card['edition'] == 'beta' and 'beta' not in (raw[:150000].lower()):
        return False
    return True


def exburst_images(doc, page_url):
    raw = html_module.unescape(doc.replace('\\/', '/').replace('\\u0026','&'))
    urls = []
    pats = [
        r'https?://cdn\.exburst\.dev/cards/cyberpunk/render-[^\s"\'<>\\]+',
        r'''<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]+content=["']([^"']+)''',
        r'''<img[^>]+src=["']([^"']+)''',
    ]
    for pat in pats:
        for val in re.findall(pat, raw, re.I):
            url = urllib.parse.urljoin(page_url, val).replace('&amp;', '&')
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname != 'cdn.exburst.dev' or '/cards/cyberpunk/render-' not in parsed.path:
                continue
            if not re.search(r'\.(webp|png|jpg|jpeg)$', parsed.path, re.I):
                continue
            if url not in urls:
                urls.append(url)
    return urls


def url_candidates(card):
    items = [card.get('image','')] + list(card.get('imageFallbacks') or [])
    unique = []
    for candidate in items:
        u = str(candidate or '')
        parsed = urllib.parse.urlsplit(u)
        if parsed.scheme == 'https' and parsed.hostname in IMAGE_HOSTS and u not in unique:
            unique.append(u)
    return unique


def mirror(card):
    key = card['key']
    dest = key_file(card)
    rel = dest.relative_to(ROOT).as_posix()
    if valid_local(dest):
        return {'key': key, 'state': 'cached', 'file': rel}
    errors=[]
    # First test this exact printing's already-catalogued URLs. Only if they
    # fail do we open a third-party card page and verify its name/printing.
    candidates = [(url, 'Katalogquelle') for url in url_candidates(card)]
    tried = set()

    def attempt(url, source):
        if url in tried:
            return None
        tried.add(url)
        try:
            data, ctype = request(url)
            if ctype and 'image/' not in ctype.lower() and 'application/octet-stream' not in ctype.lower():
                raise ValueError('Kein Bild-MIME: '+ctype)
            image = valid_image(data)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write avoids half-written images if the action is interrupted.
            temp = dest.with_suffix('.tmp')
            with WRITE_LOCK:
                image.save(temp, 'WEBP', quality=90, method=5)
                os.replace(temp, dest)
            return {'key': key, 'state':'downloaded', 'file':rel, 'source':source, 'sourceUrl':url}
        except Exception as ex:
            errors.append(f'{url[:125]}: {str(ex)[:100]}')
            return None

    for url, source in candidates:
        result = attempt(url, source)
        if result:
            return result

    # Last-resort authoritative card page, never copy a different edition.
    for page_url in page_variants(card):
        try:
            doc, ctype = request(page_url)
            if 'html' not in ctype.lower() and ctype:
                raise ValueError('Keine Kartenseite')
            doc = doc.decode('utf-8', 'replace')
            if not page_valid(doc, card, page_url):
                raise ValueError('Name/Edition/Kartennummer nicht eindeutig')
            urls = exburst_images(doc, page_url)
            for url in urls:
                result = attempt(url, page_url)
                if result:
                    return result
        except Exception as ex:
            errors.append(f'{page_url}: {str(ex)[:100]}')
    return {'key':key, 'state':'unresolved', 'card':card['name'], 'setId':card['setId'],
            'no':card['no'], 'attempted':len(tried), 'errors':errors[-5:]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--workers',type=int,default=WORKERS,help='Parallel downloads (1-12)')
    args=parser.parse_args()
    workers=max(1,min(12,args.workers))
    cards=catalog_cards()
    results={}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(mirror,c):c for c in cards}
        for future in as_completed(futures):
            c=futures[future]
            try:
                r=future.result()
            except Exception as exc:
                r={'key':c['key'],'state':'unresolved','card':c['name'],'setId':c['setId'],
                   'no':c['no'],'attempted':0,'errors':['Interner Fehler: '+str(exc)[:140]]}
            results[c['key']]=r
            with WRITE_LOCK:
                print(f"{len(results):3d}/{len(cards)} {r['state']:10s} {c['key']}",flush=True)
    complete=[results[c['key']] for c in cards if results[c['key']]['state'] != 'unresolved']
    unresolved=[results[c['key']] for c in cards if results[c['key']]['state'] == 'unresolved']
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    manifest={'schema':1,'generatedAt':now,'total':len(cards),'available':len(complete),
              'images':{item['key']:item['file'] for item in complete}}
    report={'schema':2,'generatedAt':now,'total':len(cards),'available':len(complete),
            'cached':sum(r['state']=='cached' for r in complete),
            'downloaded':sum(r['state']=='downloaded' for r in complete),
            'unresolvedCount':len(unresolved),
            'unresolved':unresolved,'newlyMirrored':[r for r in complete if r['state']=='downloaded']}
    MANIFEST.parent.mkdir(parents=True,exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f"Artwork-Abgleich abgeschlossen: {len(complete)}/{len(cards)} lokal, "
          f"{report['downloaded']} neu, {len(unresolved)} fehlen",flush=True)
    # GitHub Actions prints a readable overview of remaining failures.
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:
            f.write(f"## Night City Artwork-Abgleich\n\n"
                    f"- Geprüft: **{len(cards)}** Drucke aus 10 Sets\n"
                    f"- Lokal verfügbar: **{len(complete)}**\n"
                    f"- In diesem Lauf neu: **{report['downloaded']}**\n"
                    f"- Offen: **{len(unresolved)}**\n\n")
            if unresolved:
                f.write('### Noch fehlende Karten\n\n')
                for r in unresolved[:100]:
                    f.write(f"- `{r['key']}` — {r['card']}\n")
    if not complete:
        print('FEHLER: Keine einzigen Bilder verfügbar',file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
