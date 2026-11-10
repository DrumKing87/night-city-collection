#!/usr/bin/env python3
"""Add seven independently indexed Cyberpunk TCG releases to Night City Collection.

Runs within the existing repository. Does not rewrite existing card keys, browser
storage keys, or saved price records. Fails closed when source data changes.

Input:  original index.html, scripts/update_prices.py
Output: updated index.html, updated scripts/update_prices.py, JSON audit report
External source: public card checklist/detail pages at logtcg.app (metadata only)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, parse_qs
import json
import re
import time
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
PRICES = ROOT / "scripts/update_prices.py"
REPORT = ROOT / "data/catalog-expansion-audit.json"
BASE = "https://logtcg.app"
# Beta/Retail printings are already present; the following are NEW releases.
SOURCES = [
    dict(id="edgerunner-s1", title="Edgerunner Open S1", ed="special", kind="Promo-Set",
         code="EOR01-WNC", slug="edgerunner-open-season-1", count=53),
    dict(id="brawl-s1", title="Night City Brawl S1", ed="special", kind="Promo-Set",
         code="NCB01-WNC", slug="night-city-brawl-season-1", count=37),
    dict(id="showdown-s1", title="Night City Showdown S1", ed="special", kind="Promo-Set",
         code="NCS01-WNC", slug="night-city-showdown-season-1", count=5),
    dict(id="judge-promos", title="Judge Promos", ed="special", kind="Promo-Set",
         code="JDG-WNC", slug="judge-promos", count=3),
    dict(id="set1-promos", title="Set 1 Promos", ed="special", kind="Promo-Set",
         code="PRM01", slug="set-1-promos", count=6),
    dict(id="heist-demo", title="The Heist – Demo Deck", ed="demo", kind="Starter Deck",
         code="PRM-DD1", slug="merc-demo-deck", count=15),
    dict(id="embracing-demo", title="Embracing Power – Demo Deck", ed="demo", kind="Starter Deck",
         code="PRM-DD2", slug="arasaka-demo-deck", count=14),
]
# No verified Alpha card-by-card list yet: show set in navigator, no invented cards.
ALPHA = dict(id="alpha", title="Alpha", ed="special", kind="Promo-Set",
             code="ALPHA", source="https://www.cardmarket.com/en/Cyberpunk/Expansions")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NightCityCollection/2.0 (catalog checklists; https://github.com/DrumKing87/night-city-collection)",
                        "Accept": "text/html"})


def fetch(url):
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=(12, 30), headers=SESSION.headers)
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "text/html").lower():
                raise ValueError(f"Kein HTML: {url}")
            return BeautifulSoup(response.text, "html.parser")
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep((attempt+1)*1.5)


def plain_name(s):
    s = re.sub(r"\s*[\u2013\u2014]\s*", ": ", s).strip()
    return re.sub(r"\s+", " ", s)


def listing(spec):
    url = f"{BASE}/logpunk/sets/en/{spec['slug']}"
    soup = fetch(url)
    prefix = spec["code"] + "-"
    found = {}
    for link in soup.select('a[href*="/logpunk/cards/en/"]'):
        href = urljoin(BASE, link.get("href", ""))
        card_id = unquote(urlsplit(href).path.rsplit("/", 1)[-1])
        if not card_id.upper().startswith(prefix.upper()):
            continue
        number = card_id[len(prefix):].lower()
        if not re.fullmatch(r"\d{3}[a-z]?", number, re.I):
            continue
        title = link.get_text(" ", strip=True)
        title = re.sub(re.escape(card_id) + r"\s*$", "", title, flags=re.I).strip()
        title = title.lstrip("←→ ").strip()
        # If anchor contains only an image, the card details page supplies name.
        if card_id in found and found[card_id]["title"] and title and found[card_id]["title"] != title:
            raise ValueError(f"Widersprüchlicher Titel: {card_id}")
        found[card_id] = {"card_id": card_id, "number": number,
                          "title": title or found.get(card_id, {}).get("title", ""), "url": href}
    if len(found) != spec["count"]:
        raise ValueError(f"{spec['id']}: {len(found)} Karten statt {spec['count']}. Abbruch ohne Änderung.")
    return spec, sorted(found.values(), key=lambda item: (int(re.match(r"\d+", item["number"]).group()), item["number"]))


def image_from_page(soup, code):
    # Dedicated full-size card image, never an unrelated logo or thumbnail.
    candidates = []
    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        if code.lower() not in alt.lower():
            continue
        url = img.get("src", "") or img.get("data-src", "")
        if url.startswith("/_next/image"):
            url = unquote(parse_qs(urlsplit(url).query).get("url", [""])[0])
        url = urljoin(BASE, url)
        if url.startswith("https://"):
            candidates.append(url)
    if candidates:
        return candidates[0]
    # Some deployments expose image only in Open Graph metadata.
    for tag in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
        url = urljoin(BASE, tag.get("content", ""))
        if url.startswith("https://") and not any(x in url.lower() for x in ("logo", "favicon")):
            return url
    return ""


def details(task):
    spec, row = task
    soup = fetch(row["url"])
    text = soup.get_text(" ", strip=True)
    # Detail pages have structured human-readable fields: "Rarity Nova Rare Type Unit"
    rarity = re.search(r"\bRarity\s+(.+?)\s+Type\s+", text, flags=re.I)
    if not rarity:
        raise ValueError(f"Ohne geprüfte Seltenheit: {row['card_id']}")
    rarity = rarity.group(1).strip()
    if rarity not in {"Common", "Uncommon", "Rare", "Epic", "Secret", "Nova Rare", "Iconic Legend", "Iconic Secret", "Iconic Other"}:
        raise ValueError(f"Unbekannte Seltenheit: {row['card_id']} {rarity!r}")
    cardname = soup.find("h2")
    # Match the h2 after the h1, not H2-like section headings.
    name = row["title"]
    if cardname and cardname.get_text(" ", strip=True).lower() not in {"printings", "card text"}:
        name = cardname.get_text(" ", strip=True)
    # If source title is missing, use heading: h1 name + card identifier.
    if not name:
        h1 = soup.find("h1")
        if h1:
            name = re.sub(re.escape(row['card_id']) + r"\s*$", "", h1.get_text(" ", strip=True), flags=re.I).strip()
    if not name:
        raise ValueError(f"Kein Kartenname: {row['card_id']}")
    # Distinguish Standard and Foil from the detail page's Printings table.
    print_part = text.split("Printings", 1)[-1].split("Track this card", 1)[0]
    mfinish = re.search(r"\bPrinting\s+Where it came from\s+Where to buy\s+(Foil|Standard|Non[- ]Foil)\b", print_part, re.I)
    if not mfinish:
        raise ValueError(f"Keine sichere Druckart: {row['card_id']}")
    finish = "Holo" if mfinish.group(1).lower() == "foil" else "Normal"
    image = image_from_page(soup, row["card_id"])
    if not image:
        raise ValueError(f"Kein geprüftes Kartenbild: {row['card_id']}")
    key = spec["id"] + ":" + row["number"]
    return {
        "no": row["number"], "name": plain_name(name),
        "rarity": rarity, "finish": finish, "image": image,
        "edition": spec["ed"], "setId": spec["id"], "key": key,
    }


def replace_exact(source, old, new, *, expected=1):
    actual = source.count(old)
    if actual != expected:
        raise ValueError(f"Unsicheres Patch-Muster: {old[:85]!r}; gefunden {actual}, erwartet {expected}")
    return source.replace(old, new)


def patch_website(source, sets, cards):
    match = re.search(r"const DB\s*=\s*(\{.*?\})\s*;\s*const CARDS", source, re.S)
    if not match:
        raise ValueError("Karten-Datenbank im index.html fehlt oder hat andere Struktur")
    db = json.loads(match.group(1))
    if len(db.get("cards", [])) < 458 or len(db.get("sets", [])) < 10:
        raise ValueError("Bestehender Katalog weicht unerwartet ab")
    seen_sets = {s["id"] for s in db["sets"]}
    seen_keys = {c["key"] for c in db["cards"]}
    if len(seen_keys) != len(db["cards"]):
        raise ValueError("Karten-ID-Duplikate im bestehenden Katalog")
    new_sets = [s for s in sets if s["id"] not in seen_sets]
    new_cards = [c for c in cards if c["key"] not in seen_keys]
    if any(c["setId"] in seen_sets for c in new_cards):
        raise ValueError("Neue Karte überschreibt vorhandenes Set")
    if len(new_sets) != 8 or len(new_cards) != 133:
        raise ValueError(f"Erwartete 8 neue Sets und 133 Karten: {len(new_sets)}/{len(new_cards)}")
    db["sets"].extend(new_sets)
    db["cards"].extend(new_cards)
    modified = source[:match.start(1)] + json.dumps(db, ensure_ascii=False, separators=(",", ":")) + source[match.end(1):]
    count = len(db["cards"])
    # Static UI: old counts were incorrectly hardcoded to 458/229.
    modified = modified.replace("458 Kartendrucke aus 10 Editionen", f"{count} Kartendrucke aus 18 Sets")
    modified = modified.replace("10 Sets + Box Topper", "18 Sets + Promos")
    modified = modified.replace("458 Karten, Box Topper", f"{count} Karten, Box Topper")
    # Text-only static counter placeholders, not JS or image URLs.
    modified = modified.replace("458 Kartendrucke", f"{count} Kartendrucke")
    modified = modified.replace("458 Karten", f"{count} Karten")
    modified = modified.replace(">458</b>", f">{count}</b>")
    modified = modified.replace(">458</strong>", f">{count}</strong>")
    modified = modified.replace("0 / 458", f"0 / {count}")
    # Make the edition tabs useful for the special and demo cards.
    modified = replace_exact(modified,
        '<button type="button" class="edtab retail" data-edition="retail" aria-pressed="false">RETAIL <small>229 Karten</small></button>',
        '<button type="button" class="edtab retail" data-edition="retail" aria-pressed="false">RETAIL <small>229 Karten</small></button>'
        '<button type="button" class="edtab" data-edition="special" aria-pressed="false">PROMOS <small>104 Karten</small></button>'
        '<button type="button" class="edtab" data-edition="demo" aria-pressed="false">DEMOS <small>29 Karten</small></button>')
    modified = replace_exact(modified,
        "const labels={all:'Alle Karten',owned:'In Besitz',missing:'Fehlende Karten',dupe:'Duplikate',starter:'Starter Decks',promo:'Pre-Release',boxtopper:'Box Topper',priced:'Mit Marktpreis',unpriced:'Ohne Marktpreis'};",
        "const labels={all:'Alle Karten',owned:'In Besitz',missing:'Fehlende Karten',dupe:'Duplikate',starter:'Starter Decks',promo:'Promos & Pre-Release',boxtopper:'Box Topper',priced:'Mit Marktpreis',unpriced:'Ohne Marktpreis'};")
    modified = replace_exact(modified,
        "return s.title+' · '+(c.edition==='beta'?'β Beta':'Retail')",
        "return s.title+' · '+(c.edition==='beta'?'β Beta':c.edition==='retail'?'Retail':c.edition==='demo'?'Demo':'Promo')")
    modified = replace_exact(modified,
        "${s.ed==='beta'?'β BETA':'RETAIL'}",
        "${s.ed==='beta'?'β BETA':s.ed==='retail'?'RETAIL':s.ed==='demo'?'DEMO':'PROMO'}")
    modified = replace_exact(modified,
        "${Math.round(got/arr.length*100)} %",
        "${arr.length?Math.round(got/arr.length*100):0} %")
    modified = replace_exact(modified,
        "${got/arr.length*100}%",
        "${arr.length?got/arr.length*100:0}%")
    modified = replace_exact(modified,
        "${c.edition==='beta'?'β BETA':'RETAIL'}",
        "${c.edition==='beta'?'β BETA':c.edition==='retail'?'RETAIL':c.edition==='demo'?'DEMO':'PROMO'}")
    modified = replace_exact(modified,
        "ed==='all'?'Alle Editionen':ed==='beta'?'β Beta':'Retail'",
        "ed==='all'?'Alle Editionen':ed==='beta'?'β Beta':ed==='retail'?'Retail':ed==='demo'?'Demo':'Promos'")
    modified = replace_exact(modified,
        "${s.ed==='beta'?'β Beta':'Retail'} (${s.code})",
        "${s.ed==='beta'?'β Beta':s.ed==='retail'?'Retail':s.ed==='demo'?'Demo':'Promo'} (${s.code})")
    # Existing group semantics: promo includes the new Promo-Set entries.
    return modified


def patch_prices(source):
    source = replace_exact(source,
        "if len(cards) != 458 or len({c['key'] for c in cards}) != 458:",
        "if len(cards) < 458 or len({c['key'] for c in cards}) != len(cards):")
    source = replace_exact(source,
        "raise ValueError('Unvollständiger oder doppelter Katalog: erwarte 458 Kartendrucke')",
        "raise ValueError('Unvollständiger oder doppelter Katalog: mindestens 458 Kartendrucke erwartet')")
    source = replace_exact(source,
        "if set(c['setId'] for c in cards) != set(CARD_EXPANSION):",
        "if not set(CARD_EXPANSION).issubset({c['setId'] for c in cards}):")
    source = replace_exact(source,
        "local_groups[(CARD_EXPANSION[card['setId']], norm(card['name']))].append(card)",
        "if card['setId'] in CARD_EXPANSION:\n            local_groups[(CARD_EXPANSION[card['setId']], norm(card['name']))].append(card)")
    source = replace_exact(source,
        "        key, sid = c['key'], c['setId']\n        pair = CARD_EXPANSION[sid], norm(c['name'])",
        "        key, sid = c['key'], c['setId']\n"
        "        if sid not in CARD_EXPANSION:\n"
        "            statistics[sid]['set_ohne_verifizierte_Cardmarket_Expansion'] += 1\n"
        "            diagnostics.append({'key': key, 'name': c['name'], 'finish': c['finish'],\n"
        "                                'reason': 'set_ohne_verifizierte_Cardmarket_Expansion',\n"
        "                                'candidateProductIds': [], 'idProduct': None})\n"
        "            continue\n"
        "        pair = CARD_EXPANSION[sid], norm(c['name'])")
    return source


def main():
    original_html = INDEX.read_text(encoding="utf-8")
    original_script = PRICES.read_text(encoding="utf-8")
    # Fail on repeat runs instead of duplicating card records.
    if '"edgerunner-s1"' in original_html:
        raise ValueError("Erweiterung ist bereits installiert – keine zweite Änderung")
    sets = []
    tasks = []
    for spec in SOURCES:
        spec, records = listing(spec)
        sets.append({k: spec[k] for k in ("id", "title", "ed", "kind", "code")}
                    | {"source": f"{BASE}/logpunk/sets/en/{spec['slug']}"})
        tasks += [(spec, item) for item in records]
    sets.append(ALPHA)
    cards = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        jobs = [executor.submit(details, item) for item in tasks]
        for future in as_completed(jobs):
            cards.append(future.result())
    cards.sort(key=lambda x: (next(i for i, s in enumerate(sets) if s["id"] == x["setId"]),
                             int(re.match(r"\d+", x["no"]).group()), x["no"]))
    if len(cards) != 133 or len({x["key"] for x in cards}) != len(cards):
        raise ValueError("Uneindeutige/fehlende Drucke; keine Änderung")
    html = patch_website(original_html, sets, cards)
    script = patch_prices(original_script)
    # Write only after both scripts are validated; GitHub's git diff remains reviewable.
    INDEX.write_text(html, encoding="utf-8")
    PRICES.write_text(script, encoding="utf-8")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "originalCards": 458, "addedCards": len(cards), "resultCards": 591,
        "newSets": [{"id": x["id"], "count": sum(c["setId"] == x["id"] for c in cards)} for x in sets],
        "source": BASE, "sourceUrls": [x["source"] for x in sets],
        "notes": ["Alpha has no independently verified full checklist; zero cards added",
                  "Price importer keeps the 10 original verified Cardmarket expansion IDs;"
                  " new cards are not priced until exact product IDs/expansions are verified.",
                  "Collection owned quantities and stored prices are not modified"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("KATALOG ERWEITERT: 458 + 133 = 591 Kartendrucke, 18 Set-Einträge")
    for s in sets:
        print(s["title"], sum(c["setId"] == s["id"] for c in cards))
    print("Alle vorhandenen Kartenschlüssel und gespeicherten Preise bleiben bestehen.")


if __name__ == "__main__":
    main()
