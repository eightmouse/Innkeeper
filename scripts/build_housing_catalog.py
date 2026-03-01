"""
One-time script: Fetch all housing decor items from the server,
enrich with icons/descriptions, and save as bundled catalog.

Usage:  python scripts/build_housing_catalog.py
Output: assets/housing_decor_enriched.json
"""

import json, os, sys, time, requests

SERVER  = "https://innkeper.onrender.com"
AUTH    = "r7XkP9mQ2zW6vT4nY8sH3dFa1cJuE5LbG0tC"
HEADERS = {"X-Auth-Key": AUTH}
REGION  = "eu"
BATCH   = 25
OUT     = os.path.join(os.path.dirname(__file__), '..', 'assets', 'housing_decor_enriched.json')

def main():
    # 1. Wake up server (cold start)
    print("Waking up server (cold start may take 30-60s)...")
    for attempt in range(5):
        try:
            r = requests.get(f"{SERVER}/health", timeout=60)
            if r.status_code == 200:
                print("Server is up!")
                break
        except Exception:
            pass
        print(f"  Retrying in 10s... ({attempt+1}/5)")
        time.sleep(10)
    else:
        print("ERROR: Server did not respond. Try again later.")
        sys.exit(1)

    # 2. Fetch basic catalog
    print(f"Fetching decor index for region '{REGION}'...")
    r = requests.get(f"{SERVER}/decor/index/{REGION}", headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"ERROR: /decor/index/{REGION} returned {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    catalog = r.json()
    items = catalog.get("items", [])
    print(f"Got {len(items)} items from index.")

    # 3. Enrich in batches
    all_ids = [it["id"] for it in items if it.get("id")]
    enriched_map = {}
    total_batches = (len(all_ids) - 1) // BATCH + 1

    for i in range(0, len(all_ids), BATCH):
        batch = all_ids[i:i+BATCH]
        batch_num = i // BATCH + 1
        print(f"  Enriching batch {batch_num}/{total_batches} ({len(batch)} items)...", end=" ", flush=True)
        try:
            r = requests.post(f"{SERVER}/decor/enrich/{REGION}",
                              json={"decor_ids": batch}, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                data = r.json()
                for it in data.get("items", []):
                    enriched_map[it["decor_id"]] = it
                icons = sum(1 for it in data.get("items", []) if it.get("icon_url"))
                print(f"OK ({icons}/{len(batch)} icons)")
            else:
                print(f"FAIL ({r.status_code})")
        except Exception as e:
            print(f"ERROR: {e}")
        # Small delay to avoid rate limits
        time.sleep(0.5)

    # 4. Merge enrichment into items
    for item in items:
        info = enriched_map.get(item["id"], {})
        if info.get("icon_url"):
            item["icon_url"] = info["icon_url"]
        if info.get("description"):
            item["source"] = info["description"]
        if info.get("category") and item.get("category") == "Uncategorized":
            item["category"] = info["category"]

    # 5. Rebuild categories
    cats = ["All"]
    seen = set()
    for it in items:
        cat = it.get("category", "")
        if cat and cat not in seen:
            seen.add(cat)
            cats.append(cat)
    catalog["categories"] = cats

    # 6. Stats
    icons_total = sum(1 for it in items if it.get("icon_url"))
    descs_total = sum(1 for it in items if it.get("source"))
    cats_total  = sum(1 for it in items if it.get("category") != "Uncategorized")
    print(f"\nEnrichment complete:")
    print(f"  Items:        {len(items)}")
    print(f"  With icons:   {icons_total}")
    print(f"  With desc:    {descs_total}")
    print(f"  Categorized:  {cats_total}")

    # 7. Save
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, ensure_ascii=False)
    size_mb = os.path.getsize(OUT) / (1024 * 1024)
    print(f"\nSaved to: {os.path.abspath(OUT)} ({size_mb:.1f} MB)")

if __name__ == "__main__":
    main()
