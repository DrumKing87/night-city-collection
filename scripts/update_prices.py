#!/usr/bin/env python3
"""Night City Collection: daily official Cardmarket Cyberpunk price import.

Matches ALL 458 local printings to the Cardmarket singles catalog (game 23).
Only a uniquely matched product ID in the correct expansion and the exact
normal/foil price field is eligible. Existing valid prices survive missing data.

Duplicate card names require explicit *verified* product IDs in optional
`data/cardmarket-product-overrides.json`, e.g. {"beta:β001": 123456}.
Every override is revalidated against the product's expansion and name;
never infer an alternate-art/special-card ID from product order alone.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://downloads.s3.cardmarket.com/productCatalog'
PRODUCTS_URL = BASE + '/productList/products_singles_23.json'
PRICES_URL = BASE + '/priceGuide/price_guide_23.json'
SOURCE = 'Cardmarket (Direkt)'
LEGACY_SOURCE = 'PokéWallet / Cardmarket (Online)'
CARD_EXPANSION = {
    'wnc-beta': 6714, 'wnc-retail': 6761,
    'heist-beta': 6715, 'heist-retail': 6762,
    'embracing-beta': 6716, 'embracing-retail': 6763,
    'boxtopper-beta': 6717, 'boxtopper-retail': 6764,
    'prerelease-beta': 6718, 'prerelease-retail': 6765,
    'heist-demo': 6720, 'embracing-demo': 6721,
    'brawl-s1': 6953, 'showdown-s1': 6954, 'judge-promos': 6952, 'set1-promos': 6719,
}
DATA = ROOT / 'data'
NOW = datetime.now(timezone.utc)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def download_json(url: str) -> dict:
    req = Request(url, headers={
        'User-Agent': 'NightCityCollection/1.0 (Cardmarket-public-export-import)',
        'Accept': 'application/json',
    })
    with urlopen(req, timeout=75) as response:
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status}: {url}')
        contents = response.read(25_000_001)
    if len(contents) > 25_000_000:
        raise RuntimeError('Cardmarket-Datei zu groß (>25 MB), sicherer Abbruch')
    document = json.loads(contents)
    if not isinstance(document, dict) or document.get('version') != 1:
        raise ValueError(f'Unerwartete Cardmarket-Dateiversion in {url}')
    return document


def norm(value: str) -> str:
    text = unicodedata.normalize('NFKD', str(value or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'\(\s*v\.?\s*[12]\s*\)', '', text.lower().replace('&', 'and'), flags=re.I)
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def local_cards() -> list[dict]:
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    match = re.search(r'const DB\s*=\s*(\{.*?\})\s*;\s*const CARDS', html, re.S)
    if not match:
        raise ValueError('Lokaler Kartenkatalog in index.html nicht gefunden')
    cards = json.loads(match.group(1))['cards']
    if len(cards) < 458 or len({c['key'] for c in cards}) != len(cards):
        raise ValueError('Unvollständiger oder doppelter Katalog: mindestens 458 Kartendrucke erwartet')
    if not set(CARD_EXPANSION).issubset({c['setId'] for c in cards}):
        raise ValueError('Set-Konfiguration weicht von den Karten ab')
    return cards


def positive_price(value) -> float | None:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not (0 < amount <= 1_000_000):
            return None
        # Website stores cent amounts; use standard 2-decimal rounding.
        return float(amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return None


def cardmarket_date(document: dict) -> str:
    timestamp = document.get('createdAt')
    try:
        date = datetime.fromisoformat(timestamp)
        if not date.tzinfo:
            raise ValueError('Missing timezone')
        if date > NOW + timedelta(hours=4) or date < NOW - timedelta(days=4):
            raise ValueError('stale or future')
        return date.date().isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Cardmarket-Preisdatei hat ungültiges/veraltetes Datum: {timestamp!r}') from exc


def main() -> int:
    cards = local_cards()
    products_doc = download_json(PRODUCTS_URL)
    prices_doc = download_json(PRICES_URL)
    products, guide = products_doc.get('products'), prices_doc.get('priceGuides')
    if not isinstance(products, list) or len(products) < 400:
        raise ValueError('Cardmarket-Produktkatalog unvollständig: Abbruch ohne Änderung')
    if not isinstance(guide, list) or len(guide) < 400:
        raise ValueError('Cardmarket-Preisverzeichnis unvollständig: Abbruch ohne Änderung')
    if any(p.get('categoryName') != 'Cyberpunk Single' for p in products):
        raise ValueError('Sicherheitsstopp: Fremdkategorie im Cyberpunk-Singles-Katalog')
    as_of = cardmarket_date(prices_doc)
    products_by_id = {str(x['idProduct']): x for x in products if x.get('idProduct') is not None}
    guides_by_id = {str(x['idProduct']): x for x in guide if x.get('idProduct') is not None}
    grouped = defaultdict(list)
    for product in products:
        try:
            ex = int(product['idExpansion'])
        except (TypeError, ValueError, KeyError):
            continue
        grouped[(ex, norm(product.get('name'))) ].append(product)
    local_groups = defaultdict(list)
    for card in cards:
        if card['setId'] in CARD_EXPANSION:
            local_groups[(CARD_EXPANSION[card['setId']], norm(card['name']))].append(card)
    overrides = read_json(DATA / 'cardmarket-product-overrides.json', {})
    if not isinstance(overrides, dict):
        raise ValueError('data/cardmarket-product-overrides.json muss ein JSON-Objekt sein')
    invalid_keys = set(overrides).difference({x['key'] for x in cards})
    if invalid_keys:
        raise ValueError('Unbekannte Produkt-ID-Overrides: ' + ', '.join(sorted(invalid_keys)[:10]))
    previous = read_json(DATA / 'prices.json', {})
    if not isinstance(previous, dict) or not isinstance(previous.get('prices', {}), dict):
        raise ValueError('Bestehende Preisdatei ungültig; nichts überschrieben')
    result = dict(previous.get('prices', {}))
    statistics = defaultdict(Counter)
    diagnostics = []
    product_to_card = {}
    checked = 0
    new_values = 0
    unchanged = 0
    for c in cards:
        checked += 1
        key, sid = c['key'], c['setId']
        if sid not in CARD_EXPANSION:
            statistics[sid]['set_ohne_verifizierte_Cardmarket_Expansion'] += 1
            diagnostics.append({'key': key, 'name': c['name'], 'finish': c['finish'],
                                'reason': 'set_ohne_verifizierte_Cardmarket_Expansion',
                                'candidateProductIds': [], 'idProduct': None})
            continue
        pair = CARD_EXPANSION[sid], norm(c['name'])
        local_dupes = local_groups[pair]
        possible = grouped.get(pair, [])
        reason = None
        product = None
        if key in overrides:
            product = products_by_id.get(str(overrides[key]))
            if not product or str(product.get('idExpansion')) != str(pair[0]) or norm(product.get('name')) != pair[1]:
                raise ValueError('Ungültige oder nicht passende bestätigte Cardmarket-ID für ' + key)
        elif len(local_dupes) == 1 and len(possible) == 1:
            product = possible[0]
        elif not possible:
            reason = 'kein_Produkt_match'
        else:
            reason = 'mehrdeutiger_Sonderdruck'
        if product is not None:
            pid = str(product['idProduct'])
            if pid in product_to_card:
                raise ValueError(f'Cardmarket-Produkt {pid} wurde doppelt zugeordnet: {key} / {product_to_card[pid]}')
            product_to_card[pid] = key
            guide_row = guides_by_id.get(pid)
            if not guide_row:
                reason = 'keine_Preiszeile'
            elif str(guide_row.get('idCategory')) != str(product.get('idCategory')):
                reason = 'Kategorie_abweichend'
            else:
                finish = str(c.get('finish', '')).lower()
                field = 'trend-foil' if 'holo' in finish or 'foil' in finish else 'trend'
                amount = positive_price(guide_row.get(field))
                if amount is None or amount <= 0:
                    reason = 'kein_' + field
                else:
                    statistics[sid]['mit_preis'] += 1
                    rec = {
                        'amount': amount,
                        'currency': 'EUR',
                        'date': as_of,
                        'priceAsOf': as_of,
                        'retrievedAt': NOW.isoformat(timespec='seconds'),
                        'finish': 'foil' if field == 'trend-foil' else 'normal',
                        'source': SOURCE,
                        'idProduct': int(pid),
                    }
                    old = result.get(key, {})
                    # Do not replace any future-dated price or a custom source
                    # already stored on the server. Local browser custom prices
                    # are independently protected in index.html.
                    if isinstance(old, dict) and old.get('source') not in (None, LEGACY_SOURCE, SOURCE):
                        reason = 'serverseitiger_Eigenpreis_geschützt'
                    elif isinstance(old, dict) and str(old.get('date', '')) > as_of:
                        reason = 'bisheriger_Preis_ist_neuer'
                    else:
                        if not isinstance(old, dict) or old.get('amount') != rec['amount'] or old.get('date') != as_of or old.get('source') != SOURCE:
                            new_values += 1
                        else:
                            unchanged += 1
                        result[key] = rec
        if reason:
            statistics[sid][reason] += 1
            if len(diagnostics) < 458:
                diagnostics.append({
                    'key': key, 'name': c['name'], 'finish': c['finish'], 'reason': reason,
                    'candidateProductIds': [p.get('idProduct') for p in possible][:8],
                    'idProduct': product.get('idProduct') if product else None,
                })
    # Fail closed if an unexpected catalog structure destroys all matches.
    if sum(x['mit_preis'] for x in statistics.values()) < 250:
        raise ValueError('Weniger als 250 gültige Cardmarket-Preise: Kein Schreibzugriff')
    result_meta = {
        'schema': 1, 'currency': 'EUR', 'provider': 'Cardmarket Direkt (offizieller Tagesexport)',
        'note': 'Cardmarket EUR Trendpreise der passenden Normal/Foil-Druckart; fehlende Werte bleiben unverändert.',
        'generatedAt': NOW.isoformat(timespec='seconds'), 'sourceAsOf': as_of,
        'checkedThisRun': checked, 'mapped': len(product_to_card), 'total': len(cards),
        'updatedThisRun': new_values, 'noPriceThisRun': checked - sum(x['mit_preis'] for x in statistics.values()),
        'prices': result,
    }
    report = {
        'generatedAt': NOW.isoformat(timespec='seconds'), 'sourceAsOf': as_of,
        'total': len(cards), 'withValidCardmarketPrice': sum(x['mit_preis'] for x in statistics.values()),
        'withConfirmedProductMatch': len(product_to_card),
        'alreadySavedPriceRecords': len(previous.get('prices', {})),
        'savedPriceRecordsAfterUpdate': len(result),
        'newOrUpdatedPrices': new_values, 'bySet': {s: dict(statistics[s]) for s in CARD_EXPANSION},
        'unresolved': diagnostics,
    }
    write_json(DATA / 'prices.json', result_meta)
    write_json(DATA / 'cardmarket-diagnostics.json', report)
    print('CARDMARKET AKTUALISIERUNG:', json.dumps({
        'checked': checked, 'confirmedProductMatches': len(product_to_card),
        'validTrendPrices': report['withValidCardmarketPrice'],
        'newOrUpdated': new_values, 'unchanged': unchanged,
        'recordsInFeed': len(result), 'asOf': as_of,
    }, ensure_ascii=False))
    for sid in CARD_EXPANSION:
        print('SET', sid, json.dumps(dict(statistics[sid]), ensure_ascii=False))
    print('Nicht eindeutig zugeordnete Karten sind in data/cardmarket-diagnostics.json aufgeführt.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        print('SICHERER ABBRUCH:', e, file=sys.stderr)
        sys.exit(1)
