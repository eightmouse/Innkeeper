"""
Fetch acquisition source info for housing decorations from Wowhead.

Usage:  python scripts/fetch_housing_sources.py
Output: assets/housing_sources.json

The script:
  - Reads the enriched housing catalog
  - Searches Wowhead for each item by name (type=3 → Item)
  - Fetches the Wowhead item page and extracts source info
    (vendor, quest, drop, achievement, etc.)
  - Saves results to assets/housing_sources.json
  - Supports resuming (skips already-processed items)
"""

import json, os, sys, time, re
import urllib.request
import urllib.parse

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

CATALOG  = os.path.join(os.path.dirname(__file__), '..', 'assets', 'housing_decor_enriched.json')
STATIC   = os.path.join(os.path.dirname(__file__), '..', 'assets', 'housing_decorations.json')
OUTPUT   = os.path.join(os.path.dirname(__file__), '..', 'assets', 'housing_sources.json')
CACHE_DIR = os.path.join(os.path.dirname(__file__), '.wowhead_cache')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
RATE_LIMIT = 0.3  # seconds between Wowhead requests


# ── HTTP helpers ─────────────────────────────────────────────────

def _cache_path(url):
    """Deterministic cache filename for a URL."""
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', url)[:180]
    return os.path.join(CACHE_DIR, safe)


def fetch(url, as_json=False):
    """GET a URL, returning text (cached to disk)."""
    cp = _cache_path(url)
    if os.path.exists(cp):
        with open(cp, 'r', encoding='utf-8') as f:
            text = f.read()
        return json.loads(text) if as_json else text

    os.makedirs(CACHE_DIR, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    time.sleep(RATE_LIMIT)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"    FETCH ERROR {url[:80]}…: {e}")
        return None

    with open(cp, 'w', encoding='utf-8') as f:
        f.write(raw)
    return json.loads(raw) if as_json else raw


# ── Wowhead search ──────────────────────────────────────────────

def search_wowhead_item_id(name):
    """Search Wowhead suggestions for an Item (type 3) matching *name*."""
    q = urllib.parse.quote(name)
    url = f"https://www.wowhead.com/search/suggestions-template?id=items&q={q}"
    data = fetch(url, as_json=True)
    if not data or 'results' not in data:
        return None

    # Prefer exact name match among type==3 (Item) results
    name_lower = name.lower()
    for r in data['results']:
        if r.get('type') == 3 and r.get('name', '').lower() == name_lower:
            return r['id']
    # Fallback: first type-3 result
    for r in data['results']:
        if r.get('type') == 3:
            return r['id']
    return None


# ── Wowhead page parsing ────────────────────────────────────────

def extract_source_from_page(item_id):
    """Fetch the Wowhead item page and extract acquisition source."""
    slug = str(item_id)
    url = f"https://www.wowhead.com/item={slug}"
    html = fetch(url)
    if not html:
        return None

    sources = []

    # 1. Sold-by NPCs  ──  new Listview({...id:'sold-by'...data:[{name:'NPC',...}]})
    sold_by = _extract_listview_names(html, 'sold-by')
    if sold_by:
        sources.append("Vendor: " + ", ".join(sold_by))

    # 2. Quest reward  ──  id:'reward-from-q'
    quest_names = _extract_listview_names(html, 'reward-from-q')
    if quest_names:
        sources.append("Quest: " + ", ".join(quest_names))

    # 3. Dropped by  ──  id:'dropped-by'
    drop_names = _extract_listview_names(html, 'dropped-by')
    if drop_names:
        sources.append("Drop: " + ", ".join(drop_names[:3]))

    # 4. Contained in (object/chest)  ──  id:'contained-in-object'
    chest_names = _extract_listview_names(html, 'contained-in-object')
    if chest_names:
        sources.append("Treasure: " + ", ".join(chest_names[:3]))

    # 5. Achievement reward  ──  id:'reward-from-a'
    achieve = _extract_listview_names(html, 'reward-from-a')
    if achieve:
        sources.append("Achievement: " + ", ".join(achieve))

    # 6. Currency-for (reputation vendor, etc.)
    currency = _extract_listview_names(html, 'currency-for')
    if currency:
        sources.append("Currency: " + ", ".join(currency[:3]))

    # 7. Created by (profession recipe)  ──  id:'created-by-spell'
    crafted = _extract_listview_names(html, 'created-by-spell')
    if crafted:
        sources.append("Crafted: " + ", ".join(crafted[:3]))

    if sources:
        return " | ".join(sources)

    # Fallback: parse the decor listview's "sources" JSON array
    decor_src = _extract_decor_sources(html)
    if decor_src:
        return decor_src

    # Last resort: check for "This item can be purchased in …" text
    m = re.search(r'This item can be purchased in ([^<.]+)', html)
    if m:
        return "Vendor: " + m.group(1).strip()

    return None


# sourceType mapping from Wowhead decor data
_SOURCE_TYPES = {
    1: "Drop",
    2: "Quest",
    3: "Vendor",
    4: "Achievement",
    5: "Vendor",
    6: "Treasure",
}


def _extract_decor_sources(html):
    """Parse the decor gallery listview for structured source data."""
    m = re.search(r'"sources"\s*:\s*\[', html)
    if not m:
        return None

    # Bracket-match the sources array
    start = m.end() - 1
    depth = 0
    end = start
    for i, ch in enumerate(html[start:start+5000], start):
        if ch == '[': depth += 1
        elif ch == ']': depth -= 1
        if depth == 0:
            end = i + 1
            break

    try:
        src_arr = json.loads(html[start:end])
    except (json.JSONDecodeError, ValueError):
        return None

    parts = []
    seen = set()
    for s in src_arr:
        st = s.get('sourceType', 0)
        label = _SOURCE_TYPES.get(st, '')
        name = s.get('name', '')
        if not label or not name:
            continue
        key = f"{label}:{name}".lower()
        if key in seen:
            continue
        seen.add(key)
        area = s.get('area', {}).get('name', '')
        entry = f"{label}: {name}"
        if area:
            entry += f" ({area})"
        parts.append(entry)

    return " | ".join(parts[:4]) if parts else None


def _extract_listview_names(html, listview_id):
    """Pull NPC/quest/item names from a Wowhead Listview block."""
    pattern = rf"""["']{re.escape(listview_id)}["']"""
    m = re.search(pattern, html)
    if not m:
        return []

    # Grab a chunk after the match containing the data array
    chunk = html[m.end():m.end()+12000]

    # Find the data:[ ... ] block — the data is JSON-style
    data_m = re.search(r'data\s*:\s*\[', chunk)
    if not data_m:
        return []

    # Extract the JSON array by bracket matching
    start = data_m.end() - 1  # include the opening [
    depth = 0
    end = start
    for i, ch in enumerate(chunk[start:], start):
        if ch == '[': depth += 1
        elif ch == ']': depth -= 1
        if depth == 0:
            end = i + 1
            break

    json_str = chunk[start:end]
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        # Fallback: regex for "name":"..." patterns
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', json_str)
        return _dedup(names)

    # Extract name field from each object
    names = [obj.get('name', '') for obj in data if isinstance(obj, dict) and obj.get('name')]
    return _dedup(names)


def _dedup(names):
    """De-duplicate names while preserving order."""
    seen = set()
    unique = []
    for n in names:
        nl = n.lower()
        if nl not in seen:
            seen.add(nl)
            unique.append(n)
    return unique


# ── Main ─────────────────────────────────────────────────────────

def main():
    # Load catalogs
    if not os.path.exists(CATALOG):
        print(f"ERROR: Catalog not found at {CATALOG}")
        print("Run build_housing_catalog.py first.")
        sys.exit(1)

    with open(CATALOG, 'r', encoding='utf-8') as f:
        catalog = json.load(f)

    # Craftable item names (from static catalog) — skip these
    craftable_names = set()
    if os.path.exists(STATIC):
        with open(STATIC, 'r', encoding='utf-8') as f:
            static = json.load(f)
        craftable_names = {it['name'].lower() for it in static.get('items', [])}

    # Load existing output (for resuming)
    sources = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, 'r', encoding='utf-8') as f:
            sources = json.load(f)

    items = catalog.get('items', [])
    to_process = [it for it in items if it['name'].lower() not in craftable_names]

    total = len(to_process)
    already = sum(1 for it in to_process if it['name'] in sources)
    print(f"Total items: {len(items)}  |  Non-craftable: {total}  |  Already done: {already}")
    print(f"Output: {OUTPUT}\n")

    processed = 0
    for i, item in enumerate(to_process):
        name = item['name']
        if name in sources:
            continue

        processed += 1
        pct = int((i + 1) / total * 100)
        print(f"[{i+1}/{total}  {pct}%] {name}")

        # Step 1: Search Wowhead for the Item ID
        wh_id = search_wowhead_item_id(name)
        if not wh_id:
            print(f"    ✗ Not found on Wowhead")
            sources[name] = ""
            continue

        # Step 2: Fetch item page & extract source
        source = extract_source_from_page(wh_id)
        if source:
            print(f"    ✓ {source[:80]}")
        else:
            print(f"    – No source info (wh={wh_id})")

        sources[name] = source or ""

        # Save progress every 25 items
        if processed % 25 == 0:
            _save(sources)
            print(f"    [Saved {len(sources)} entries]")

    _save(sources)

    found = sum(1 for v in sources.values() if v)
    print(f"\nDone!  {len(sources)} items processed, {found} with source info.")


def _save(sources):
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(sources, f, indent=2, ensure_ascii=False)


if __name__ == '__main__':
    main()
