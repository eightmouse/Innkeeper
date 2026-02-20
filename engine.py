# Innkeeper - Version 0.9
# @Author: eightmouse

# ------------[      MODULES      ]------------ #
import json, requests, os, sys
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

UTC     = timezone.utc
basedir = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))
load_dotenv(os.path.join(basedir, '.env'))

client_id     = os.getenv("BLIZZARD_CLIENT_ID")
client_secret = os.getenv("BLIZZARD_CLIENT_SECRET")
DATA_FILE     = os.path.join(basedir, 'characters.json')

# ------------[  DATA PERSISTENCE  ]------------ #

def emit(data):
    print(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()

def save_data(characters):
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump([c.to_dict() for c in characters], f, indent=4, ensure_ascii=False)
        print(f"[engine] Saved {len(characters)} chars → {DATA_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"[engine] SAVE ERROR: {e}", file=sys.stderr)

def load_data():
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        return [Character.from_dict(c) for c in json.loads(content)] if content else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []

# ------------[        API        ]------------ #

_token_cache: dict[str, str] = {}

def get_access_token(region: str = "eu") -> str | None:
    if region in _token_cache:
        return _token_cache[region]
    r = requests.post(f"https://{region}.battle.net/oauth/token",
                      data={"grant_type": "client_credentials"},
                      auth=(client_id, client_secret))
    if r.status_code == 200:
        _token_cache[region] = r.json()["access_token"]
        return _token_cache[region]
    print(f"[engine] Token error {region}: {r.status_code}", file=sys.stderr)
    return None

def _headers(token):
    return {"Authorization": f"Bearer {token}"}

def _params(region, locale="en_US", namespace_prefix="profile"):
    return {"namespace": f"{namespace_prefix}-{region}", "locale": locale}

def _slug(realm: str) -> str:
    """Convert a realm display name to its URL slug."""
    return realm.lower().replace(" ", "-").replace("'", "").replace(".", "")

def fetch_character(region: str, realm: str, name: str, token: str) -> dict | None:
    """Fetch full character profile — level, class, active_spec, etc."""
    r = requests.get(
        f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{name.lower()}",
        params=_params(region), headers=_headers(token)
    )
    if r.status_code == 200: return r.json()
    print(f"[engine] fetch_character {r.status_code}: {r.text[:200]}", file=sys.stderr)
    return None

def fetch_character_media(region: str, realm: str, name: str, token: str) -> dict:
    """
    Returns { 'render': <full scene URL>, 'avatar': <bust portrait URL> }.
    The character-media endpoint includes:
    - 'render' (if available): full Armory scene with class background
    - 'main-raw' or 'main': character cutout with class background
    - 'avatar': small bust portrait
    """
    r = requests.get(
        f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{name.lower()}/character-media",
        params=_params(region), headers=_headers(token)
    )
    if r.status_code != 200:
        print(f"[engine] fetch_media {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return {}
    assets = {a["key"]: a["value"] for a in r.json().get("assets", [])}
    result = {}
    # Priority: render > main-raw > main (render is the full armory scene)
    for key in ("render", "main-raw", "main"):
        if key in assets:
            result["render"] = assets[key]
            break
    if "avatar" in assets:
        result["avatar"] = assets["avatar"]
    return result

def fetch_equipment(region: str, realm: str, name: str, token: str) -> list:
    """
    Returns a list of equipped items:
    [ { slot, name, ilvl, quality, icon_url }, ... ]
    Icons are fetched from the static item-media endpoint.
    """
    r = requests.get(
        f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{name.lower()}/equipment",
        params=_params(region), headers=_headers(token)
    )
    if r.status_code != 200:
        print(f"[engine] fetch_equipment {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []

    items = []
    for item in r.json().get("equipped_items", []):
        slot    = item.get("slot", {}).get("type", "")
        quality = item.get("quality", {}).get("type", "COMMON")
        ilvl    = item.get("level", {}).get("value", 0)
        iname   = item.get("name", "")
        item_id = item.get("item", {}).get("id")

        icon_url = None
        if item_id:
            mr = requests.get(
                f"https://{region}.api.blizzard.com/data/wow/media/item/{item_id}",
                params=_params(region, namespace_prefix="static"),
                headers=_headers(token)
            )
            if mr.status_code == 200:
                icon_assets = {a["key"]: a["value"] for a in mr.json().get("assets", [])}
                icon_url = icon_assets.get("icon")

        items.append({"slot": slot, "name": iname, "ilvl": ilvl,
                      "quality": quality, "icon_url": icon_url})
    return items

def fetch_realms(region: str, token: str) -> list[str]:
    """Return sorted list of realm display names for the given region."""
    r = requests.get(
        f"https://{region}.api.blizzard.com/data/wow/realm/index",
        params=_params(region, namespace_prefix="dynamic"),
        headers=_headers(token)
    )
    if r.status_code == 200:
        return sorted([realm["name"] for realm in r.json()["realms"]])
    print(f"[engine] fetch_realms {r.status_code}", file=sys.stderr)
    return []

def _build_character(data: dict, region: str, realm: str, name: str, token: str) -> "Character":
    """Assemble a Character from a raw Blizzard profile response."""
    cls      = data.get("character_class", {})
    spec     = data.get("active_spec", {})
    media    = fetch_character_media(region, realm, name, token)
    avg_ilvl = data.get("average_item_level", 0)
    
    # Map class and spec to WoWHead slugs for talent backgrounds
    class_slug = _get_class_slug(cls.get("id"))
    spec_slug  = _get_spec_slug(spec.get("name", ""))
    
    return Character(
        name, data.get("level", "?"), realm, region,
        portrait_url = media.get("render"),
        avatar_url   = media.get("avatar"),
        class_id     = cls.get("id"),
        class_name   = cls.get("name", ""),
        spec_name    = spec.get("name", ""),
        class_slug   = class_slug,
        spec_slug    = spec_slug,
        item_level   = avg_ilvl,
    )

def _get_class_slug(class_id):
    """Map Blizzard class ID to WoWHead slug."""
    return {
        1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue",
        5: "priest", 6: "death-knight", 7: "shaman", 8: "mage",
        9: "warlock", 10: "monk", 11: "druid", 12: "demon-hunter", 13: "evoker"
    }.get(class_id, "warrior")

def _get_spec_slug(spec_name):
    """Convert spec name to WoWHead slug."""
    return spec_name.lower().replace(" ", "-") if spec_name else ""

def auto_add_character(region: str, name: str) -> "Character | None":
    """Scan every realm in the region until the character is found."""
    token = get_access_token(region)
    if not token: return None
    realm_names = fetch_realms(region, token)
    if not realm_names: return None
    print(f"[engine] Scanning {len(realm_names)} realms for {name}…", file=sys.stderr)
    for i, realm_name in enumerate(realm_names):
        print(f"[engine] {i+1}/{len(realm_names)} {realm_name}", file=sys.stderr)
        data = fetch_character(region, realm_name, name, token)
        if data:
            return _build_character(data, region, realm_name, name, token)
    print(f"[engine] {name} not found in {region}", file=sys.stderr)
    return None

# ------------[       CLASS       ]------------ #

class Character:
    def __init__(self, name, level, realm, region="eu",
                 portrait_url=None, avatar_url=None,
                 class_id=None, class_name="", spec_name="",
                 class_slug="", spec_slug="", item_level=0):
        self.name         = name
        self.level        = level
        self.realm        = realm
        self.region       = region
        self.portrait_url = portrait_url
        self.avatar_url   = avatar_url
        self.class_id     = class_id
        self.class_name   = class_name
        self.spec_name    = spec_name
        self.class_slug   = class_slug
        self.spec_slug    = spec_slug
        self.item_level   = item_level
        self.equipment    = []
        self.equipment_last_check = None
        self.activities   = {
            "Raid":         {"status": "available", "reset": "weekly"},
            "Mythic+":      {"status": "available", "reset": "weekly"},
            "Expeditions":  {"status": "available", "reset": "weekly"},
            "World Quests": {"status": "available", "reset": "daily"},
        }
        self.last_reset_check = datetime.now(UTC)

    # ── Reset logic ──────────────────────────────────────

    def get_last_reset_boundary(self, reset_type: str) -> datetime:
        """Most recent 08:00 UTC boundary for the given reset type (daily or weekly)."""
        now      = datetime.now(UTC)
        boundary = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if reset_type == "daily":
            if now < boundary: boundary -= timedelta(days=1)
            return boundary
        days_since_wed = (now.weekday() - 2) % 7
        boundary      -= timedelta(days=days_since_wed)
        if now < boundary: boundary -= timedelta(days=7)
        return boundary

    def check_resets(self):
        daily_b  = self.get_last_reset_boundary("daily")
        weekly_b = self.get_last_reset_boundary("weekly")
        modified = False
        for data in self.activities.values():
            boundary = weekly_b if data["reset"] == "weekly" else daily_b
            if self.last_reset_check < boundary:
                data["status"] = "available"
                modified = True
        if modified:
            self.last_reset_check = datetime.now(UTC)

    # ── Mutation ─────────────────────────────────────────

    def toggle_activity(self, activity_name: str):
        if activity_name in self.activities:
            cur = self.activities[activity_name]["status"]
            self.activities[activity_name]["status"] = "completed" if cur == "available" else "available"

    # ── Serialisation ────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "name":                  self.name,
            "level":                 self.level,
            "realm":                 self.realm,
            "region":                self.region,
            "portrait_url":          self.portrait_url,
            "avatar_url":            self.avatar_url,
            "class_id":              self.class_id,
            "class_name":            self.class_name,
            "spec_name":             self.spec_name,
            "class_slug":            self.class_slug,
            "spec_slug":             self.spec_slug,
            "item_level":            self.item_level,
            "equipment":             self.equipment,
            "equipment_last_check":  self.equipment_last_check.isoformat() if self.equipment_last_check else None,
            "activities":            self.activities,
            "last_reset_check":      self.last_reset_check.isoformat(),
        }

    @staticmethod
    def from_dict(d: dict) -> "Character":
        char = Character(
            d["name"], d["level"], d["realm"],
            d.get("region", "eu"),
            portrait_url = d.get("portrait_url"),
            avatar_url   = d.get("avatar_url"),
            class_id     = d.get("class_id"),
            class_name   = d.get("class_name", ""),
            spec_name    = d.get("spec_name", ""),
            class_slug   = d.get("class_slug", ""),
            spec_slug    = d.get("spec_slug", ""),
            item_level   = d.get("item_level", 0),
        )
        char.equipment       = d.get("equipment", [])
        char.equipment_last_check = datetime.fromisoformat(d["equipment_last_check"]) if d.get("equipment_last_check") else None
        char.activities       = d["activities"]
        char.last_reset_check = datetime.fromisoformat(
            d.get("last_reset_check", datetime.now(UTC).isoformat()))
        return char

# ------------[       MAIN        ]------------ #

def find_character(characters, name, realm):
    n, r = name.lower(), realm.lower()
    return next((c for c in characters if c.name.lower() == n and c.realm.lower() == r), None)



# ------------[  HELPER FUNCTION   ]------------ #

# ------------[  TALENT TREE API  ]------------ #

SPECS_MAP = {
    'warrior': ['arms', 'fury', 'protection'],
    'paladin': ['holy', 'protection', 'retribution'],
    'hunter': ['beast-mastery', 'marksmanship', 'survival'],
    'rogue': ['assassination', 'outlaw', 'subtlety'],
    'priest': ['discipline', 'holy', 'shadow'],
    'death-knight': ['blood', 'frost', 'unholy'],
    'shaman': ['elemental', 'enhancement', 'restoration'],
    'mage': ['arcane', 'fire', 'frost'],
    'warlock': ['affliction', 'demonology', 'destruction'],
    'monk': ['brewmaster', 'mistweaver', 'windwalker'],
    'druid': ['balance', 'feral', 'guardian', 'restoration'],
    'demon-hunter': ['havoc', 'vengeance', 'devourer'],
    'evoker': ['devastation', 'preservation', 'augmentation']
}

# Blizzard numeric spec IDs
SPEC_IDS = {
    'warrior':      {'arms': 71, 'fury': 72, 'protection': 73},
    'paladin':      {'holy': 65, 'protection': 66, 'retribution': 70},
    'hunter':       {'beast-mastery': 253, 'marksmanship': 254, 'survival': 255},
    'rogue':        {'assassination': 259, 'outlaw': 260, 'subtlety': 261},
    'priest':       {'discipline': 256, 'holy': 257, 'shadow': 258},
    'death-knight': {'blood': 250, 'frost': 251, 'unholy': 252},
    'shaman':       {'elemental': 262, 'enhancement': 263, 'restoration': 264},
    'mage':         {'arcane': 62, 'fire': 63, 'frost': 64},
    'warlock':      {'affliction': 265, 'demonology': 266, 'destruction': 267},
    'monk':         {'brewmaster': 268, 'mistweaver': 270, 'windwalker': 269},
    'druid':        {'balance': 102, 'feral': 103, 'guardian': 104, 'restoration': 105},
    'demon-hunter': {'havoc': 577, 'vengeance': 581, 'devourer': 1480},
    'evoker':       {'devastation': 1467, 'preservation': 1468, 'augmentation': 1473},
}

def fetch_talent_tree(region, class_slug, spec_slug):
    """Fetch the full talent tree structure from Blizzard API, with caching."""
    spec_id = SPEC_IDS.get(class_slug, {}).get(spec_slug)
    if not spec_id:
        raise ValueError(f"Unknown spec: {class_slug}/{spec_slug} — not in SPEC_IDS")

    cache_dir = os.path.join(basedir, 'talent_tree_cache')
    cache_file = os.path.join(cache_dir, f'{class_slug}_{spec_slug}.json')

    # Return cached data if available
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            # Validate cache has expected structure
            if cached.get('class_nodes') and cached.get('spec_nodes'):
                print(f"[engine] Loaded cached talent tree: {cache_file}", file=sys.stderr)
                return cached
            else:
                print(f"[engine] Cache file corrupt, re-fetching…", file=sys.stderr)
        except Exception as e:
            print(f"[engine] Cache read error: {e}", file=sys.stderr)

    token = get_access_token(region)
    if not token:
        raise ConnectionError(f"Failed to get Blizzard API token for region '{region}'. Check BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET in your .env file.")

    # Get spec info to find the talent tree ID
    r = requests.get(
        f"https://{region}.api.blizzard.com/data/wow/playable-specialization/{spec_id}",
        params={"namespace": f"static-{region}", "locale": "en_US"},
        headers=_headers(token),
        timeout=15,
    )
    if r.status_code != 200:
        raise ConnectionError(f"Step 1 failed: Blizzard API returned {r.status_code} for spec {spec_id}. Response: {r.text[:200]}")

    spec_data = r.json()
    
    # Blizzard API uses 'spec_talent_tree' (not old 'talent_trees')
    # This field can be: an int, a dict with key.href, or a dict with id
    spec_tree_ref = spec_data.get('spec_talent_tree')
    if not spec_tree_ref:
        talent_trees = spec_data.get('talent_trees', [])
        if talent_trees:
            spec_tree_ref = talent_trees[0]
        else:
            raise ValueError(f"Step 1: No talent tree reference found. Keys: {list(spec_data.keys())}")
    
    tree_id = None
    
    if isinstance(spec_tree_ref, int):
        # Direct integer ID
        tree_id = spec_tree_ref
    elif isinstance(spec_tree_ref, dict):
        # Try key.href first: {"key":{"href":"https://...talent-tree/786/..."}}
        tree_href = ''
        key_obj = spec_tree_ref.get('key')
        if isinstance(key_obj, dict):
            tree_href = key_obj.get('href', '')
        elif isinstance(key_obj, str):
            tree_href = key_obj
        
        if '/talent-tree/' in tree_href:
            parts = tree_href.split('/talent-tree/')[1].split('/')
            try:
                tree_id = int(parts[0])
            except (ValueError, IndexError):
                pass
        
        # Fallback to direct id field
        if not tree_id:
            tree_id = spec_tree_ref.get('id')
    
    if not tree_id:
        raise ValueError(f"Step 1: Could not extract tree ID. spec_talent_tree value: {spec_tree_ref}")
    
    # Fetch the spec-filtered talent tree
    r2 = requests.get(
        f"https://{region}.api.blizzard.com/data/wow/talent-tree/{tree_id}/playable-specialization/{spec_id}",
        params={"namespace": f"static-{region}", "locale": "en_US"},
        headers=_headers(token),
        timeout=15,
    )
    if r2.status_code != 200:
        raise ConnectionError(f"Step 2 failed: Blizzard API returned {r2.status_code} for tree {tree_id}. Response: {r2.text[:200]}")

    raw_tree = r2.json()

    # Collect ALL node IDs across every spec of this class (export string encodes the full tree).
    # Fetch each sibling spec's tree and merge node IDs for complete coverage.
    all_node_ids = set()
    def _collect_node_ids(tree_json):
        for node in tree_json.get('class_talent_nodes', []):
            if isinstance(node, dict) and 'id' in node:
                all_node_ids.add(node['id'])
        for node in tree_json.get('spec_talent_nodes', []):
            if isinstance(node, dict) and 'id' in node:
                all_node_ids.add(node['id'])
        for ht in tree_json.get('hero_talent_trees', []):
            if isinstance(ht, dict):
                for node in ht.get('hero_talent_nodes', []):
                    if isinstance(node, dict) and 'id' in node:
                        all_node_ids.add(node['id'])

    # Start with the current spec's raw response
    _collect_node_ids(raw_tree)

    # Fetch sibling specs to get their unique node IDs
    sibling_specs = SPEC_IDS.get(class_slug, {})
    for sib_slug, sib_id in sibling_specs.items():
        if sib_id == spec_id:
            continue
        try:
            r_sib = requests.get(
                f"https://{region}.api.blizzard.com/data/wow/talent-tree/{tree_id}/playable-specialization/{sib_id}",
                params={"namespace": f"static-{region}", "locale": "en_US"},
                headers=_headers(token),
                timeout=15,
            )
            if r_sib.status_code == 200:
                _collect_node_ids(r_sib.json())
        except Exception:
            pass

    all_node_ids_sorted = sorted(all_node_ids)

    # Parse into a simpler format (pass spec_id for filtering)
    parsed = _parse_talent_tree(raw_tree, spec_id)
    parsed['all_node_ids'] = all_node_ids_sorted
    class_count = len(parsed.get('class_nodes', []))
    spec_count = len(parsed.get('spec_nodes', []))
    hero_count = sum(len(ht.get('nodes', [])) for ht in parsed.get('hero_trees', []))
    print(f"[engine] Talent tree: {class_count} class + {spec_count} spec + {hero_count} hero nodes ({len(all_node_ids_sorted)} total IDs)", file=sys.stderr)

    if class_count == 0 and spec_count == 0:
        raise ValueError(f"Parsed tree has 0 nodes. Raw tree keys: {list(raw_tree.keys())}")

    # Fetch spell icons for all entries
    _attach_spell_icons(parsed, region, token)

    # Cache to disk
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(parsed, f, indent=2, ensure_ascii=False)
    print(f"[engine] ✓ Cached talent tree → {cache_file}", file=sys.stderr)

    return parsed


def _attach_spell_icons(parsed, region, token):
    """Batch-fetch spell icon URLs and attach them to every entry in the tree."""
    all_spell_ids = set()
    all_nodes = list(parsed.get('class_nodes', [])) + list(parsed.get('spec_nodes', []))
    for ht in parsed.get('hero_trees', []):
        if isinstance(ht, dict):
            all_nodes += ht.get('nodes', [])
    for node in all_nodes:
        if not isinstance(node, dict):
            continue
        for entry in node.get('entries', []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get('spell_id')
            if sid and isinstance(sid, int):
                all_spell_ids.add(sid)

    if not all_spell_ids:
        return

    print(f"[engine] Fetching {len(all_spell_ids)} spell icons in parallel…", file=sys.stderr)

    icon_map = {}

    def _fetch_one_icon(spell_id):
        try:
            r = requests.get(
                f"https://{region}.api.blizzard.com/data/wow/media/spell/{spell_id}",
                params={"namespace": f"static-{region}", "locale": "en_US"},
                headers=_headers(token),
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                assets = data.get("assets", [])
                if isinstance(assets, list):
                    for a in assets:
                        if isinstance(a, dict) and a.get("key") == "icon":
                            return (spell_id, a.get("value"))
        except Exception:
            pass
        return (spell_id, None)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(_fetch_one_icon, sid) for sid in all_spell_ids]
        for future in as_completed(futures):
            sid, url = future.result()
            if url:
                icon_map[sid] = url

    # Attach icons to entries
    for node in all_nodes:
        if not isinstance(node, dict):
            continue
        for entry in node.get('entries', []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get('spell_id')
            if sid and sid in icon_map:
                entry['icon_url'] = icon_map[sid]

    print(f"[engine] Got icons for {len(icon_map)}/{len(all_spell_ids)} spells", file=sys.stderr)


def _parse_talent_tree(raw, active_spec_id=None):
    """Parse Blizzard's talent tree response into a frontend-friendly format.
    The API already returns only nodes for the queried spec.
    We filter hero trees to only include ones available to the active spec."""
    result = {
        'class_nodes': [],
        'spec_nodes': [],
        'hero_trees': [],
    }

    # Parse class nodes (shared across all specs of this class)
    # IMPORTANT: Keep ALL nodes including empty gates — decoder needs them for bit alignment
    for node in raw.get('class_talent_nodes', []):
        if isinstance(node, dict):
            parsed_node = _parse_node(node)
            result['class_nodes'].append(parsed_node)

    # Parse spec nodes (API already returns only the queried spec's nodes)
    for node in raw.get('spec_talent_nodes', []):
        if isinstance(node, dict):
            parsed_node = _parse_node(node)
            result['spec_nodes'].append(parsed_node)

    # Parse hero talent trees — filter to only include trees for the active spec
    hero_raw = raw.get('hero_talent_trees', [])
    if isinstance(hero_raw, list):
        for ht in hero_raw:
            if not isinstance(ht, dict):
                continue
            hero_nodes_raw = ht.get('hero_talent_nodes', [])
            if not isinstance(hero_nodes_raw, list):
                hero_nodes_raw = []

            # Filter by playable_specializations
            ht_specs = ht.get('playable_specializations', [])
            if ht_specs and active_spec_id:
                spec_ids_in_tree = set()
                for sp_ref in ht_specs:
                    if isinstance(sp_ref, int):
                        spec_ids_in_tree.add(sp_ref)
                    elif isinstance(sp_ref, dict):
                        sid = sp_ref.get('id')
                        if sid is not None:
                            spec_ids_in_tree.add(int(sid))
                if spec_ids_in_tree and active_spec_id not in spec_ids_in_tree:
                    continue

            hero = {
                'id': ht.get('id', 0) if isinstance(ht.get('id'), int) else 0,
                'name': ht.get('name', '') if isinstance(ht.get('name'), str) else str(ht.get('name', '')),
                'nodes': [_parse_node(n) for n in hero_nodes_raw if isinstance(n, dict)]
            }
            result['hero_trees'].append(hero)

    # Sort by ID (critical for build string decoding)
    result['class_nodes'].sort(key=lambda n: n['id'])
    result['spec_nodes'].sort(key=lambda n: n['id'])

    return result


def _safe_get(obj, key, default=None):
    """Safely get a key from obj — handles obj being int, str, list, or None."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _parse_node(node):
    """Parse a single talent node into simplified format.
    Defensively handles API fields being int/str instead of dict."""
    
    # node_type can be {"type":"ACTIVE"} or a string "ACTIVE" or an int
    raw_type = node.get('node_type', 'ACTIVE')
    if isinstance(raw_type, dict):
        node_type = raw_type.get('type', 'ACTIVE')
    elif isinstance(raw_type, str):
        node_type = raw_type
    else:
        node_type = 'ACTIVE'
    
    # locked_by can be [{id: 123}] or [123] or [{"id": 123}]
    raw_deps = node.get('locked_by', [])
    locked_by = []
    for dep in raw_deps:
        if isinstance(dep, int):
            locked_by.append(dep)
        elif isinstance(dep, dict):
            locked_by.append(dep.get('id', 0))
        else:
            pass  # skip unknown formats

    n = {
        'id':         node.get('id', 0),
        'row':        node.get('display_row', 0),
        'col':        node.get('display_col', 0),
        'pos_x':      node.get('raw_position_x', 0),
        'pos_y':      node.get('raw_position_y', 0),
        'type':       node_type,
        'max_ranks':  0,
        'entries':    [],
        'locked_by':  locked_by,
    }

    ranks = node.get('ranks', [])
    n['max_ranks'] = max(len(ranks), 1)

    # Check for choice nodes (have choice_of_tooltips)
    if ranks and isinstance(ranks[0], dict) and ranks[0].get('choice_of_tooltips'):
        n['type'] = 'CHOICE'
        n['max_ranks'] = 1
        for ct in ranks[0]['choice_of_tooltips']:
            if not isinstance(ct, dict):
                continue
            st = _safe_get(ct, 'spell_tooltip', {})
            sp = _safe_get(st, 'spell', {})
            talent_ref = _safe_get(ct, 'talent', {})
            n['entries'].append({
                'name':        _safe_get(sp, 'name') or _safe_get(talent_ref, 'name', '?'),
                'spell_id':    _safe_get(sp, 'id', 0) if isinstance(sp, dict) else (sp if isinstance(sp, int) else 0),
                'description': _safe_get(st, 'description', ''),
                'cast_time':   _safe_get(st, 'cast_time', ''),
                'cooldown':    _safe_get(st, 'cooldown', ''),
                'range':       _safe_get(st, 'range', ''),
            })
    else:
        # Regular node — one entry per rank
        for rank_info in ranks:
            if not isinstance(rank_info, dict):
                continue
            tt = _safe_get(rank_info, 'tooltip', {})
            st = _safe_get(tt, 'spell_tooltip', {})
            sp = _safe_get(st, 'spell', {})
            talent_ref = _safe_get(tt, 'talent', {})
            n['entries'].append({
                'name':        _safe_get(sp, 'name') or _safe_get(talent_ref, 'name', '?'),
                'spell_id':    _safe_get(sp, 'id', 0) if isinstance(sp, dict) else (sp if isinstance(sp, int) else 0),
                'description': _safe_get(st, 'description', ''),
                'cast_time':   _safe_get(st, 'cast_time', ''),
                'cooldown':    _safe_get(st, 'cooldown', ''),
                'range':       _safe_get(st, 'range', ''),
            })

    return n


# ------------[    MAIN LOOP       ]------------ #

def main():
    emit({"status": "ready"})

    if not client_id or not client_secret:
        emit({"status": "error",
              "message": "Blizzard API credentials missing — check your .env file"})

    characters = load_data()
    for char in characters:
        char.check_resets()
    save_data(characters)

    for raw_line in sys.stdin:
        command = raw_line.strip()
        if not command:
            continue

        if command == "GET_CHARACTERS":
            emit([c.to_dict() for c in characters])

        elif command.startswith("GET_REALMS:"):
            region = command.split(":", 1)[1].strip()
            token  = get_access_token(region)
            realms = fetch_realms(region, token) if token else []
            emit({"status": "realms", "region": region, "realms": realms})

        elif command.startswith("ADD_CHARACTER:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                token = get_access_token(region)
                if not token:
                    emit({"status": "error", "message": "Could not authenticate with Blizzard API"})
                else:
                    data = fetch_character(region, realm, name, token)
                    if data:
                        char = _build_character(data, region, realm, name, token)
                        if not find_character(characters, char.name, char.realm):
                            characters.append(char)
                            save_data(characters)
                        emit({"status": "added", "character": char.to_dict()})
                    else:
                        emit({"status": "not_found"})

        elif command.startswith("AUTO_ADD:"):
            parts = command.split(":", 2)
            if len(parts) == 3:
                _, region, name = [p.strip() for p in parts]
                char = auto_add_character(region, name)
                if char:
                    if not find_character(characters, char.name, char.realm):
                        characters.append(char)
                        save_data(characters)
                    emit({"status": "added", "character": char.to_dict()})
                else:
                    emit({"status": "not_found"})

        elif command.startswith("DELETE_CHARACTER:"):
            parts = command.split(":", 2)
            if len(parts) == 3:
                _, name, realm = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    characters.remove(char)
                    save_data(characters)
                    emit({"status": "deleted", "name": name, "realm": realm})

        elif command.startswith("TOGGLE_ACTIVITY:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, name, realm, activity = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    char.toggle_activity(activity)
                    save_data(characters)
                    new_status = char.activities.get(activity, {}).get("status")
                    emit({"status": "toggled", "name": name,
                          "activity": activity, "new_status": new_status})

        elif command.startswith("GET_EQUIPMENT:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                
                if char and char.equipment and char.equipment_last_check:
                    cache_age = (datetime.now(UTC) - char.equipment_last_check).total_seconds()
                    if cache_age < 300:
                        emit({"status": "equipment", "name": name, "realm": realm,
                              "items": char.equipment, "cached": True})
                        continue
                
                token = get_access_token(region)
                if token:
                    items = fetch_equipment(region, realm, name, token)
                    if char and items:
                        char.equipment = items
                        char.equipment_last_check = datetime.now(UTC)
                        save_data(characters)
                    emit({"status": "equipment", "name": name, "realm": realm,
                          "items": items, "cached": False})
                else:
                    emit({"status": "error", "message": "Could not authenticate"})

        elif command.startswith("REFRESH_EQUIPMENT:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                token = get_access_token(region)
                if token:
                    emit({"status": "equipment_refreshing", "name": name, "realm": realm})
                    items = fetch_equipment(region, realm, name, token)
                    if char:
                        char.equipment = items
                        char.equipment_last_check = datetime.now(UTC)
                        save_data(characters)
                    emit({"status": "equipment", "name": name, "realm": realm,
                          "items": items, "cached": False})
                else:
                    emit({"status": "error", "message": "Could not authenticate"})

        elif command.startswith("FETCH_TALENT_TREE:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, class_slug, spec_slug = [p.strip() for p in parts]
                try:
                    tree = fetch_talent_tree(region, class_slug, spec_slug)
                    if tree:
                        emit({"status": "talent_tree", "class_slug": class_slug,
                              "spec_slug": spec_slug, "tree": tree})
                    else:
                        emit({"status": "talent_tree_error",
                              "class_slug": class_slug, "spec_slug": spec_slug,
                              "message": f"API returned no data for {class_slug}/{spec_slug}. Check API credentials in .env"})
                except Exception as e:
                    print(f"[engine] Talent tree fetch crashed: {e}", file=sys.stderr)
                    emit({"status": "talent_tree_error",
                          "class_slug": class_slug, "spec_slug": spec_slug,
                          "message": str(e)})

        elif command == "CLEAR_TALENT_CACHE":
            cache_dir = os.path.join(basedir, 'talent_tree_cache')
            if os.path.exists(cache_dir):
                import shutil
                shutil.rmtree(cache_dir)
            emit({"status": "success", "message": "Talent tree cache cleared"})

        elif command == "EXIT":
            save_data(characters)
            break


if __name__ == "__main__":
    main()