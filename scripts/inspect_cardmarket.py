#!/usr/bin/env python3
"""Read-only diagnosis of Cardmarket Cyberpunk (game 23) public JSON exports.

This script never modifies the user collection or price files. It checks the
actual product and price-guide schema before a future price importer is built.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
import unicodedata
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GAME = 23
BASE = 'https://downloads.s3.cardmarket.com/productCatalog'
PRODUCTS_URL = f'{BASE}/productList/products_singles_{GAME}.json'
PRICES_URL = f'{BASE}/priceGuide/price_guide_{GAME}.json'


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        'User-Agent': 'NightCityCollection/1.0 (read-only Cardmarket feed verification)',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=75) as response:
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status} for {url}')
        data = response.read(60_000_001)
        if len(data) > 60_000_000:
            raise RuntimeError('Datei überschreitet die Sicherheitsgrenze (60 MB).')
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError('Download ist kein JSON-Objekt.')
    return obj


def normalize(value: str) -> str:
    text = unicodedata.normalize('NFKD', str(value or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace('&', 'and')
    text = re.sub(r'\(\s*v\.?\s*[12]\s*\)', '', text, flags=re.I)
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def load_local_cards() -> list[dict]:
    html = (ROOT / 'index.html').read_text('utf-8')
    m = re.search(r'const DB\s*=\s*(\{.*?\})\s*;\s*const CARDS', html, re.S)
    if not m:
        raise ValueError('Kartenkatalog in index.html fehlt.')
    cards = json.loads(m.group(1))['cards']
    if len(cards) != 458:
        raise ValueError(f'Unerwartete Kartenzahl: {len(cards)} statt 458')
    return cards


def main() -> int:
    cards = load_local_cards()
    print(f'Prüfe {len(cards)} lokale Karten; KEINE Änderung an Preisdateien.')
    print(f'Cardmarket Spiel-ID {GAME}; Produktkatalog: {PRODUCTS_URL}')
    products_doc = fetch_json(PRODUCTS_URL)
    products = products_doc.get('products')
    if not isinstance(products, list):
        raise ValueError(f'Produktkatalog ohne products[]; Schlüssel: {list(products_doc)}')
    print('Produktkatalog:', json.dumps({
        'version': products_doc.get('version'),
        'createdAt': products_doc.get('createdAt'),
        'products': len(products),
        'sampleKeys': sorted(products[0]) if products else [],
        'categories': Counter(str(x.get('categoryName') or x.get('idCategory')) for x in products).most_common(8),
        'sampleNames': [str(x.get('name')) for x in products[:8]],
    }, ensure_ascii=False))
    if not products:
        raise ValueError('Keine Produkte vorhanden')
    # A number alone is not proof of the right game. Require the game/category to say Cyberpunk.
    names = ' '.join(str(p.get('categoryName', '')) for p in products[:200]).lower()
    if 'cyberpunk' not in names:
        raise ValueError('SICHERHEITSSTOPP: Produktkatalog nicht als Cyberpunk identifiziert.')

    print(f'Cardmarket Spiel-ID {GAME}; Preisdatei: {PRICES_URL}')
    prices_doc = fetch_json(PRICES_URL)
    guides = prices_doc.get('priceGuides')
    if not isinstance(guides, list):
        raise ValueError(f'Preisdatei ohne priceGuides[]; Schlüssel: {list(prices_doc)}')
    print('Preisdatei:', json.dumps({
        'version': prices_doc.get('version'),
        'createdAt': prices_doc.get('createdAt'),
        'priceRows': len(guides),
        'sampleKeys': sorted(guides[0]) if guides else [],
    }, ensure_ascii=False))
    if not guides:
        raise ValueError('Keine Preise vorhanden')

    by_id = {str(g.get('idProduct')): g for g in guides if g.get('idProduct') is not None}
    joined = [(p, by_id[str(p['idProduct'])]) for p in products
              if p.get('idProduct') is not None and str(p['idProduct']) in by_id]
    def usable(v):
        try:
            return 0 < float(v) <= 1_000_000
        except (TypeError, ValueError):
            return False
    print('Preiskatalog-Abgleich:', json.dumps({
        'productsWithPriceRow': len(joined),
        'withNormalTrend': sum(usable(g.get('trend')) for _, g in joined),
        'withFoilTrend': sum(usable(g.get('trend-foil')) for _, g in joined),
        'withHoloTrend': sum(usable(g.get('trend-holo')) for _, g in joined),
    }, ensure_ascii=False))

    # Diagnostic only: potential expansion IDs by comparing product names to our cards.
    local_by_name = defaultdict(set)
    for c in cards:
        local_by_name[normalize(c['name'])].add(c['setId'])
    expansions = defaultdict(lambda: Counter())
    for p in products:
        name = normalize(p.get('name'))
        for set_id in local_by_name.get(name, ()):
            expansions[str(p.get('idExpansion', '(ohne)'))][set_id] += 1
    for expansion, counts in sorted(expansions.items(), key=lambda it: -sum(it[1].values()))[:18]:
        print('Mögliche Expansion', expansion, 'Namensüberschneidungen:',
              json.dumps(dict(counts.most_common()), ensure_ascii=False))
    print('ERFOLG: Dateien überprüft. Noch KEINE Preise übernommen.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (urllib.error.URLError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print('DIAGNOSE ABGEBROCHEN:', str(exc), file=sys.stderr)
        sys.exit(1)
