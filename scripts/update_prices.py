#!/usr/bin/env python3
"""Conservatively update Night City Collection price feed via the PokéWallet API.

No name-only guesses. Exact edition + collector number + card name required.
Only Cardmarket EUR trend prices for the exact normal/foil finish are retained.
This script is intended for GitHub Actions (never put API credentials in the repo).
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
API = 'https://api.pokewallet.io/cpk'
TODAY = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
MAX_PER_RUN = 60  # 8 runs daily, at most 60 card details each; free tier 100 requests/hour.
MAPPING_REFRESH_DAYS = 1  # Recheck source mappings daily for newly released cards.

SET_NAMES = {
 'wnc-beta': 'Welcome to Night City - Beta',
 'wnc-retail': 'Welcome to Night City - Retail',
 'heist-beta': 'The Heist - Beta Starter Deck',
 'heist-retail': 'The Heist - Retail Starter Deck',
 'embracing-beta': 'Embracing Power - Beta Starter Deck',
 'embracing-retail': 'Embracing Power - Retail Starter Deck',
 'prerelease-beta': 'Pre-Release Beta',
 'prerelease-retail': 'Pre-Release Retail',
 'boxtopper-beta': 'Box Toppers - Beta',
 'boxtopper-retail': 'Box Toppers - Retail',
}

def normalize(s):
 s = unicodedata.normalize('NFKD', str(s or ''))
 s = ''.join(ch for ch in s if not unicodedata.combining(ch)).lower().replace('&', 'and')
 # Third-party sources append print classifications, not printed card name.
 s = re.sub(r'\(\s*(?:v\.?\s*[12]|i(?:secret|legend|other)|epic|rare|secret|foil)\s*\)', '', s, flags=re.I)
 s = re.sub(r'[^a-z0-9]+', ' ', s)
 return ' '.join(s.split())

def collector(s):
 s = str(s or '').lower().strip().replace('β', 'b')
 m = re.fullmatch(r'b?0*(\d+)([a-z]?)', s)
 return (int(m.group(1)), m.group(2)) if m else None

def load_json(path, fallback):
 try: return json.loads(path.read_text('utf-8'))
 except FileNotFoundError: return fallback

def dump_json(path, data):
 path.parent.mkdir(parents=True, exist_ok=True)
 path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n', 'utf-8')

def catalog():
 html = (ROOT / 'index.html').read_text('utf-8')
 match = re.search(r'const DB\s*=\s*(\{.*?\})\s*;\s*const CARDS', html, re.S)
 if not match: raise ValueError('Kartenkatalog in index.html nicht gefunden.')
 cards = json.loads(match.group(1))['cards']
 if len(cards) < 400: raise ValueError('Unerwartet wenige Karten, Abbruch.')
 return cards

def request(path, params=None):
 key = os.environ.get('POKEWALLET_API_KEY', '').strip()
 if not key: raise RuntimeError('GitHub-Secret POKEWALLET_API_KEY fehlt. Kein Preisupdate möglich.')
 url = API + path
 if params: url += '?' + urllib.parse.urlencode(params)
 req = urllib.request.Request(url, headers={'X-API-Key': key, 'Accept': 'application/json', 'User-Agent': 'NightCityCollection/1.0 personal-card-tracker'})
 for attempt in range(3):
  try:
   with urllib.request.urlopen(req, timeout=25) as response:
    if response.status != 200: raise RuntimeError('API HTTP '+str(response.status))
    data = json.load(response)
   if not isinstance(data, dict): raise ValueError('API: ungültiges JSON')
   return data
  except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as ex:
   status = getattr(ex, 'code', 0)
   if status in (401, 403, 429):
    raise RuntimeError(f'API Fehler HTTP {status}. Secret bzw. Freikontingent prüfen.') from ex
   if attempt == 2: raise
   time.sleep(2 + attempt * 3)
 raise AssertionError('unreachable')

def build_card_index(cards):
 """Match exact edition, set ID, number and name; never guess between prints."""
 sets_res = request('/sets')
 api_sets = sets_res.get('data', [])
 resolved = {}
 for sid, name in SET_NAMES.items():
  ids = sorted({str(s['set_id']) for s in api_sets if s.get('set_id') is not None and normalize(s.get('name')) == normalize(name)})
  if ids:
   resolved[sid] = ids
   if len(ids) > 1:
    print(f'Mehrere API-Sets für {name}; prüfe Karten einzeln auf eindeutige Drucke: {len(ids)} Sets')
  else:
   print(f'Noch kein exakt passendes API-Set: {name}')
 index = {}
 for sid, set_ids in resolved.items():
  all_entries = []
  for set_id in set_ids:
   for page in range(1, 21):
    response = request('/sets/' + urllib.parse.quote(set_id, safe=''), {'page': page, 'limit': 200})
    entries = response.get('cards', [])
    if not isinstance(entries, list): raise ValueError('API: Kartenliste fehlt')
    for ent in entries:
     info = ent.get('card_info') or {}
     if str(info.get('set_id')) != set_id: continue
     all_entries.append((set_id, ent))
    paging = response.get('pagination') or {}
    if page >= int(paging.get('total_pages', 1)): break
  local = [c for c in cards if c['setId'] == sid]
  for card in local:
   wanted_n = collector(card['no'])
   wanted_name = normalize(card['name'])
   candidates = []
   for entry_set_id, ent in all_entries:
    info = ent.get('card_info') or {}
    actual_n = collector(info.get('card_number'))
    if wanted_n is None or actual_n is None: continue
    if wanted_n[0] != actual_n[0]: continue
    if wanted_n[1] and actual_n[1] and wanted_n[1] != actual_n[1]: continue
    if wanted_name != normalize(info.get('name')): continue
    if wanted_n[1] and not actual_n[1]:
     suffix = wanted_n[1]
     version = re.search(r'\(\s*v\.?\s*([12])\s*\)', str(info.get('name')), flags=re.I)
     if suffix in ('a', 'b') and (not version or int(version.group(1)) != ('ab'.index(suffix) + 1)):
      continue
    if info.get('product_type') == 'sealed': continue
    eid = ent.get('id')
    if not isinstance(eid, str) or not re.fullmatch(r'(?:cpk_[a-f0-9]{16,128}|[a-f0-9]{64})', eid): continue
    candidates.append({'id': eid, 'set_id': entry_set_id, 'name': str(info.get('name')), 'number': str(info.get('card_number'))})
   if len(candidates) == 1: index[card['key']] = candidates[0]
   elif len(candidates) > 1: print(f'Mehrdeutiger Druck, nicht zugeordnet: {card["key"]}: {len(candidates)}')
 for sid in SET_NAMES:
  local = [c for c in cards if c['setId'] == sid]
  missing = [c['no'] for c in local if c['key'] not in index]
  print(f'{sid}: {len(local)-len(missing)}/{len(local)} zugeordnet' +
        ('; offen: ' + ', '.join(missing[:10]) + (' ...' if len(missing)>10 else '') if missing else ''))
 print(f'{len(index)} von {len(cards)} Drucke eindeutig mit PokéWallet verknüpft')
 return index

def valid_amount(v):
 try:
  n = float(v)
  return round(n, 2) if 0 < n <= 1_000_000 else None
 except (TypeError, ValueError): return None

def price_from_detail(local_card, match, detail):
 info = detail.get('card_info') or {}
 if str(info.get('set_id')) != match['set_id']:
  return None
 wanted_n = collector(local_card['no'])
 actual_n = collector(info.get('card_number'))
 if wanted_n is None or actual_n is None or wanted_n[0] != actual_n[0]: return None
 if wanted_n[1] and actual_n[1] and wanted_n[1] != actual_n[1]: return None
 if wanted_n[1] and not actual_n[1]:
  version = re.search(r'\(\s*v\.?\s*([12])\s*\)', str(info.get('name')), flags=re.I)
  if wanted_n[1] in ('a', 'b') and (not version or int(version.group(1)) != ('ab'.index(wanted_n[1]) + 1)):
   return None
 if normalize(info.get('name')) != normalize(local_card['name']): return None
 cm = detail.get('cardmarket') or {}
 want = 'foil' if 'holo' in str(local_card.get('finish', '')).lower() else 'normal'
 choices = [p for p in cm.get('prices', []) if str(p.get('variant_type', '')).lower() == want]
 if len(choices) != 1: return None
 row = choices[0]
 amount = valid_amount(row.get('trend'))
 if amount is None: return None  # Asking prices are not sale trend; do NOT substitute lowest listing.
 updated_at = str(row.get('updated_at') or '')
 return {'amount': amount, 'currency': 'EUR', 'source': 'PokéWallet / Cardmarket (Online)',
         'date': TODAY[:10], 'priceAsOf': updated_at[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', updated_at) else None,
         'retrievedAt': TODAY, 'finish': want}

def main():
 cards = catalog()
 by_key = {c['key']: c for c in cards}
 cache = ROOT / 'data/card-map.json'
 state_path = ROOT / 'data/update-state.json'
 prices_path = ROOT / 'data/prices.json'
 state = load_json(state_path, {'cursor': 0})
 cache_data = load_json(cache, {'mapping': {}, 'generatedAt': None})
 mapping = cache_data.get('mapping') or {}
 # Refresh mapping daily as more Retail and promo singles reach the API.
 stamp = cache_data.get('generatedAt', '') or ''
 try:
  age = (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(stamp[:10])).days
 except (ValueError, TypeError):
  age = MAPPING_REFRESH_DAYS
 if cache_data.get('matcherVersion') != 3 or not mapping or age >= MAPPING_REFRESH_DAYS:
  fresh = build_card_index(cards)
  if not fresh: raise RuntimeError('Kein eindeutiger Produktabgleich möglich; keine Preise geändert.')
  # Upstream may temporarily omit a product; retain prior verified matches.
  mapping = {**{k: v for k, v in mapping.items() if k in by_key}, **fresh}
  dump_json(cache, {'mapping': mapping, 'generatedAt': TODAY, 'total': len(mapping), 'matcherVersion': 3})
 keys = [c['key'] for c in cards if c['key'] in mapping]
 if not keys: raise RuntimeError('Kein einziges Kartenprodukt zugeordnet.')
 cursor = int(state.get('cursor', 0)) % len(keys)
 batch = [keys[(cursor+i) % len(keys)] for i in range(min(MAX_PER_RUN, len(keys)))]
 existing = load_json(prices_path, {'schema': 1, 'prices': {}})
 result = {k: v for k, v in (existing.get('prices') or {}).items() if k in by_key}
 checks = updated = misses = 0
 for key in batch:
  try:
   detail = request('/cards/' + urllib.parse.quote(mapping[key]['id'], safe=''))
   price = price_from_detail(by_key[key], mapping[key], detail)
   if price:
    result[key] = price
    updated += 1
   else: misses += 1
   checks += 1
  except Exception as ex:
   print('Abrufproblem:', key, str(ex)[:160], file=sys.stderr)
   # Rate-limit / authentication errors are critical and must NOT publish a false success.
   if 'HTTP 429' in str(ex) or 'HTTP 401' in str(ex) or 'HTTP 403' in str(ex):
    raise
  time.sleep(0.15)
 if checks == 0: raise RuntimeError('Kein Preisabruf erfolgreich; vorherige Datei beibehalten.')
 dump_json(prices_path, {'schema': 1, 'provider': 'PokéWallet / Cardmarket',
                          'generatedAt': TODAY, 'currency': 'EUR', 'prices': result,
                          'mapped': len(mapping), 'total': len(cards), 'checkedThisRun': checks,
                          'updatedThisRun': updated, 'noPriceThisRun': misses,
                          'note': 'Cardmarket EUR Trendpreise nach exaktem Druck und Finish; kein garantiert erzielbarer Verkaufspreis.'})
 dump_json(state_path, {'cursor': (cursor + len(batch)) % len(keys), 'lastRun': TODAY,
                        'mapped': len(mapping), 'lastChecked': checks, 'lastNewPrices': updated})
 print(f'Karten zugeordnet: {len(mapping)}/{len(cards)}; geprüft: {checks}; verwertbare Preise: {updated}; gespeichert: {len(result)}')

if __name__ == '__main__':
 try: main()
 except Exception as exc:
  print(f'Preisupdate NICHT veröffentlicht: {exc}', file=sys.stderr)
  sys.exit(1)
