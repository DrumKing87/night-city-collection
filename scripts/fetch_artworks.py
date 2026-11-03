#!/usr/bin/env python3
"""Retrieve matching retail card renders for Night City Collection.

The script only saves correctly shaped, decodable card artwork from card
pages for retail 144-171. It does not infer a retail image from a beta image.
It records failures instead of creating misleading replacements.
"""

from __future__ import annotations

import html
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "assets" / "cards" / "retail"
REPORT = ROOT / "data" / "artwork-report.json"
SOURCE_HTML = ROOT / "index.html"
OUTDIR.mkdir(parents=True, exist_ok=True)
REPORT.parent.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (compatible; NightCityCollectionArtworkCheck/1.1)"
IMAGE_HOSTS = {"cdn.exburst.dev", "exburst.dev"}
TIMEOUT = 25


def fetch_bytes(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        size = response.headers.get("Content-Length")
        if size and int(size) > 10_000_000:
            raise ValueError("image/page larger than 10MB")
        body = response.read(10_000_001)
        if len(body) > 10_000_000:
            raise ValueError("image/page exceeds 10MB")
        return body, response.headers.get("content-type", "")


def candidates(page_html: str, base_url: str) -> list[str]:
    """Accept only card-render asset URLs, not generic social preview banners."""
    raw = html.unescape(page_html.replace("\\/", "/").replace("\\u0026", "&"))
    urls = []
    # ExBurst embeds rendered card URLs in both meta tags and its page data.
    patterns = [
        r"https?://cdn\.exburst\.dev/cards/cyberpunk/render-[^\s\"'<>\\]+",
        r'''<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]+content=["']([^"']+)''',
        r'''<img[^>]+src=["']([^"']+)''',
    ]
    for pattern in patterns:
        for candidate in re.findall(pattern, raw, re.IGNORECASE):
            url = urllib.parse.urljoin(base_url, candidate).replace("&amp;", "&")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
                continue
            if "/cards/cyberpunk/render-" not in parsed.path:
                continue
            if not re.search(r"\.(?:webp|png|jpg|jpeg)$", parsed.path, re.I):
                continue
            if url not in urls:
                urls.append(url)
    return urls


def catalog_images() -> dict[str, str]:
    """Keep already catalogued specific Retail renders as alternative candidates."""
    text = SOURCE_HTML.read_text(encoding="utf-8")
    m = re.search(r"const DB\s*=\s*(\{.*?\})\s*;\s*const CARDS", text, re.S)
    if not m:
        return {}
    cards = json.loads(m.group(1))["cards"]
    found = {}
    for c in cards:
        if c.get("edition") != "retail" or not str(c.get("no", "")).isdigit():
            continue
        url = str(c.get("image", ""))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS and "/cards/cyberpunk/render-" in parsed.path:
            found[str(c["no"])] = url
    return found


def decode_card(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = im.size
            ratio = width / height
            # Reject tiny thumbnails and landscape/site banners.
            if width < 240 or height < 330 or not (0.58 <= ratio <= 0.86):
                raise ValueError(f"not card-sized image ({width} x {height})")
            return im.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("not a valid image") from exc


def main() -> int:
    saved = catalog_images()
    report = {"generatedAt": datetime.now(timezone.utc).isoformat(), "fetched": [], "failed": []}
    for n in range(144, 172):
        number = str(n)
        page = f"https://exburst.dev/cyberpunk/cards/MS01-{number}"
        key = f"retail:{number}"
        urls = []
        errors = []
        try:
            doc_bytes, _ = fetch_bytes(page)
            urls = candidates(doc_bytes.decode("utf-8", "ignore"), page)
        except Exception as exc:
            errors.append("page: " + str(exc)[:180])
        if number in saved and saved[number] not in urls:
            urls.append(saved[number])
        completed = False
        for url in urls:
            try:
                data, ctype = fetch_bytes(url)
                if ctype and not any(x in ctype.lower() for x in ("image/webp", "image/png", "image/jpeg", "application/octet-stream")):
                    raise ValueError("not an image content type: " + ctype)
                picture = decode_card(data)
                dest = OUTDIR / (number + ".webp")
                picture.save(dest, "WEBP", quality=91, method=5)
                report["fetched"].append({"key": key, "sourcePage": page, "imageUrl": url, "file": str(dest.relative_to(ROOT))})
                print(f"OK {key} -> {dest.relative_to(ROOT)}")
                completed = True
                break
            except Exception as exc:
                errors.append("asset: " + str(exc)[:180])
        if not completed:
            report["failed"].append({"key": key, "sourcePage": page, "tried": len(urls), "errors": errors[-4:]})
            print(f"MISSING {key}: no validated card image (tried {len(urls)} URLs)")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = len(report["fetched"])
    print(f"Validated retail artworks: {ok}/28; failed: {len(report['failed'])}")
    # Failing entirely is intentional: otherwise a green run falsely signals fixed images.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
