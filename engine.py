# Innkeeper - Version 1.0
# @Author: eightmouse

# ------------[      MODULES      ]------------ #
import json, requests, os, sys, shutil
from datetime import datetime, timedelta, timezone

UTC     = timezone.utc
basedir = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))

SERVER_URL = "https://innkeper.onrender.com"
AUTH_KEY   = "InnkeeperApp-2026"
DATA_FILE  = os.path.join(basedir, 'characters.json')

# ============================================================
#  FASTAPI SERVER  (only when imported by uvicorn on Render)
# ============================================================

if __name__ != "__main__":
    import time, asyncio
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from dotenv import load_dotenv
    from fastapi import FastAPI, HTTPException, Request

    load_dotenv()

    BLIZZARD_CLIENT_ID     = os.getenv("BLIZZARD_CLIENT_ID")
    BLIZZARD_CLIENT_SECRET = os.getenv("BLIZZARD_CLIENT_SECRET")
    AUTH_KEY               = os.getenv("AUTH_KEY", "")

    app = FastAPI(title="Innkeeper API", version="1.0")

    # ────────────────────  Auth middleware  ──────────────────────

    @app.middleware("http")
    async def _check_auth(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if request.headers.get("X-Auth-Key") != AUTH_KEY:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return await call_next(request)

    # ────────────────────  Token Management  ────────────────────

    _token_cache: dict[str, dict] = {}
    TOKEN_TTL = 24 * 3600 - 300

    def get_access_token(region: str = "eu") -> str | None:
        cached = _token_cache.get(region)
        if cached and cached["expires"] > time.time():
            return cached["token"]
        r = requests.post(
            f"https://{region}.battle.net/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET),
            timeout=10,
        )
        if r.status_code == 200:
            token = r.json()["access_token"]
            _token_cache[region] = {"token": token, "expires": time.time() + TOKEN_TTL}
            return token
        return None

    # ────────────────────  Blizzard HTTP helper  ────────────────

    def _blizzard_get(url, params, token, timeout=15):
        r = requests.get(url, params=params,
                         headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None

    def _params(region, locale="en_US", namespace_prefix="profile"):
        return {"namespace": f"{namespace_prefix}-{region}", "locale": locale}

    def _slug(realm):
        return realm.lower().replace(" ", "-").replace("'", "").replace(".", "")

    # ────────────────────  Lookup maps  ─────────────────────────

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

    def _get_class_slug(class_id):
        return {
            1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue",
            5: "priest", 6: "death-knight", 7: "shaman", 8: "mage",
            9: "warlock", 10: "monk", 11: "druid", 12: "demon-hunter", 13: "evoker"
        }.get(class_id, "warrior")

    def _get_spec_slug(spec_name):
        return spec_name.lower().replace(" ", "-") if spec_name else ""

    # ────────────────────  Blizzard data helpers  ───────────────

    def _fetch_character(region, realm, name, token):
        return _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{name.lower()}",
            _params(region), token)

    def _fetch_character_media(region, realm, name, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{name.lower()}/character-media",
            _params(region), token)
        if not data:
            return {}
        assets = {a["key"]: a["value"] for a in data.get("assets", [])}
        result = {}
        for key in ("render", "main-raw", "main"):
            if key in assets:
                result["render"] = assets[key]
                break
        if "avatar" in assets:
            result["avatar"] = assets["avatar"]
        return result

    def _build_character_dict(data, region, realm, name, token):
        cls   = data.get("character_class", {})
        spec  = data.get("active_spec", {})
        media = _fetch_character_media(region, realm, name, token)
        return {
            "name":         data.get("name", name),
            "level":        data.get("level", "?"),
            "realm":        realm,
            "region":       region,
            "portrait_url": media.get("render"),
            "avatar_url":   media.get("avatar"),
            "class_id":     cls.get("id"),
            "class_name":   cls.get("name", ""),
            "spec_name":    spec.get("name", ""),
            "class_slug":   _get_class_slug(cls.get("id")),
            "spec_slug":    _get_spec_slug(spec.get("name", "")),
            "item_level":   data.get("average_item_level", 0),
        }

    def _fetch_realms(region, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/data/wow/realm/index",
            _params(region, namespace_prefix="dynamic"), token)
        if data:
            return sorted([r["name"] for r in data["realms"]])
        return []

    def _fetch_equipment(region, realm, name, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{name.lower()}/equipment",
            _params(region), token)
        if not data:
            return []

        items_basic = []
        for item in data.get("equipped_items", []):
            slot    = item.get("slot", {}).get("type", "")
            quality = item.get("quality", {}).get("type", "COMMON")
            ilvl    = item.get("level", {}).get("value", 0)
            iname   = item.get("name", "")
            item_id = item.get("item", {}).get("id")
            items_basic.append({"slot": slot, "name": iname, "ilvl": ilvl,
                                "quality": quality, "icon_url": None, "_item_id": item_id})

        def _fetch_icon(item_id):
            mr = _blizzard_get(
                f"https://{region}.api.blizzard.com/data/wow/media/item/{item_id}",
                _params(region, namespace_prefix="static"), token, timeout=10)
            if mr:
                icon_assets = {a["key"]: a["value"] for a in mr.get("assets", [])}
                return icon_assets.get("icon")
            return None

        ids_to_fetch = [(i, it["_item_id"]) for i, it in enumerate(items_basic) if it["_item_id"]]
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(_fetch_icon, item_id): idx for idx, item_id in ids_to_fetch}
            for future in as_completed(futures):
                idx = futures[future]
                items_basic[idx]["icon_url"] = future.result()

        for it in items_basic:
            it.pop("_item_id", None)
        return items_basic

    # ────────────────────  Talent tree helpers  ─────────────────

    def _safe_get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default

    def _parse_node(node):
        raw_type = node.get('node_type', 'ACTIVE')
        if isinstance(raw_type, dict):
            node_type = raw_type.get('type', 'ACTIVE')
        elif isinstance(raw_type, str):
            node_type = raw_type
        else:
            node_type = 'ACTIVE'

        raw_deps = node.get('locked_by', [])
        locked_by = []
        for dep in raw_deps:
            if isinstance(dep, int):
                locked_by.append(dep)
            elif isinstance(dep, dict):
                locked_by.append(dep.get('id', 0))

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

    def _parse_talent_tree(raw, active_spec_id=None):
        result = {'class_nodes': [], 'spec_nodes': [], 'hero_trees': []}

        for node in raw.get('class_talent_nodes', []):
            if isinstance(node, dict):
                result['class_nodes'].append(_parse_node(node))

        for node in raw.get('spec_talent_nodes', []):
            if isinstance(node, dict):
                result['spec_nodes'].append(_parse_node(node))

        hero_raw = raw.get('hero_talent_trees', [])
        if isinstance(hero_raw, list):
            for ht in hero_raw:
                if not isinstance(ht, dict):
                    continue
                hero_nodes_raw = ht.get('hero_talent_nodes', [])
                if not isinstance(hero_nodes_raw, list):
                    hero_nodes_raw = []

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
                    'id':    ht.get('id', 0) if isinstance(ht.get('id'), int) else 0,
                    'name':  ht.get('name', '') if isinstance(ht.get('name'), str) else str(ht.get('name', '')),
                    'nodes': [_parse_node(n) for n in hero_nodes_raw if isinstance(n, dict)]
                }
                result['hero_trees'].append(hero)

        result['class_nodes'].sort(key=lambda n: n['id'])
        result['spec_nodes'].sort(key=lambda n: n['id'])
        return result

    def _attach_spell_icons(parsed, region, token):
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

        icon_map = {}

        def _fetch_one_icon(spell_id):
            try:
                r = requests.get(
                    f"https://{region}.api.blizzard.com/data/wow/media/spell/{spell_id}",
                    params={"namespace": f"static-{region}", "locale": "en_US"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10)
                if r.status_code == 200:
                    for a in r.json().get("assets", []):
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

        for node in all_nodes:
            if not isinstance(node, dict):
                continue
            for entry in node.get('entries', []):
                if not isinstance(entry, dict):
                    continue
                sid = entry.get('spell_id')
                if sid and sid in icon_map:
                    entry['icon_url'] = icon_map[sid]

    def _fetch_talent_tree_from_blizzard(region, class_slug, spec_slug):
        spec_id = SPEC_IDS.get(class_slug, {}).get(spec_slug)
        if not spec_id:
            raise ValueError(f"Unknown spec: {class_slug}/{spec_slug}")

        token = get_access_token(region)
        if not token:
            raise ConnectionError("Failed to get Blizzard API token")

        spec_data = _blizzard_get(
            f"https://{region}.api.blizzard.com/data/wow/playable-specialization/{spec_id}",
            {"namespace": f"static-{region}", "locale": "en_US"}, token)
        if not spec_data:
            raise ConnectionError(f"Blizzard API returned no data for spec {spec_id}")

        spec_tree_ref = spec_data.get('spec_talent_tree')
        if not spec_tree_ref:
            talent_trees = spec_data.get('talent_trees', [])
            if talent_trees:
                spec_tree_ref = talent_trees[0]
            else:
                raise ValueError(f"No talent tree reference found. Keys: {list(spec_data.keys())}")

        tree_id = None
        if isinstance(spec_tree_ref, int):
            tree_id = spec_tree_ref
        elif isinstance(spec_tree_ref, dict):
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
            if not tree_id:
                tree_id = spec_tree_ref.get('id')

        if not tree_id:
            raise ValueError(f"Could not extract tree ID from: {spec_tree_ref}")

        raw_tree = _blizzard_get(
            f"https://{region}.api.blizzard.com/data/wow/talent-tree/{tree_id}/playable-specialization/{spec_id}",
            {"namespace": f"static-{region}", "locale": "en_US"}, token)
        if not raw_tree:
            raise ConnectionError(f"Blizzard API returned no data for tree {tree_id}")

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

        _collect_node_ids(raw_tree)

        sibling_specs = SPEC_IDS.get(class_slug, {})
        for sib_slug, sib_id in sibling_specs.items():
            if sib_id == spec_id:
                continue
            try:
                sib_data = _blizzard_get(
                    f"https://{region}.api.blizzard.com/data/wow/talent-tree/{tree_id}/playable-specialization/{sib_id}",
                    {"namespace": f"static-{region}", "locale": "en_US"}, token)
                if sib_data:
                    _collect_node_ids(sib_data)
            except Exception:
                pass

        parsed = _parse_talent_tree(raw_tree, spec_id)
        parsed['all_node_ids'] = sorted(all_node_ids)

        _attach_spell_icons(parsed, region, token)
        return parsed

    # ────────────────────  Auto-add semaphore  ──────────────────

    _auto_add_semaphore = asyncio.Semaphore(1)

    # ────────────────────  Endpoints  ───────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/realms/{region}")
    async def realms(region: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return {"region": region, "realms": _fetch_realms(region, token)}

    @app.get("/character/{region}/{realm}/{name}")
    async def character(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        data = _fetch_character(region, realm, name, token)
        if not data:
            raise HTTPException(404, f"Character {name} not found on {realm}-{region}")
        return _build_character_dict(data, region, realm, name, token)

    @app.get("/equipment/{region}/{realm}/{name}")
    async def equipment(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return _fetch_equipment(region, realm, name, token)

    @app.get("/talent-tree/{region}/{class_slug}/{spec_slug}")
    async def talent_tree(region: str, class_slug: str, spec_slug: str):
        try:
            return _fetch_talent_tree_from_blizzard(region, class_slug, spec_slug)
        except (ValueError, ConnectionError) as e:
            raise HTTPException(400, str(e))

    @app.post("/auto-add")
    async def auto_add(body: dict):
        region = body.get("region", "eu")
        name   = body.get("name", "")
        if not name:
            raise HTTPException(400, "Missing 'name'")
        if _auto_add_semaphore.locked():
            raise HTTPException(429, "Another auto-add scan is already running. Try again later.")
        async with _auto_add_semaphore:
            token = get_access_token(region)
            if not token:
                raise HTTPException(502, "Failed to get Blizzard API token")
            realm_names = _fetch_realms(region, token)
            if not realm_names:
                raise HTTPException(502, "Could not fetch realm list")
            for realm_name in realm_names:
                data = _fetch_character(region, realm_name, name, token)
                if data:
                    return _build_character_dict(data, region, realm_name, name, token)
            raise HTTPException(404, f"Character '{name}' not found in any {region} realm")

# ============================================================
#  LOCAL CLIENT  (runs via python engine.py in Electron app)
# ============================================================

# ────────────────────  Server HTTP helpers  ─────────────────

_AUTH_HEADERS = {"X-Auth-Key": AUTH_KEY}

def _server_get(path, timeout=30):
    try:
        r = requests.get(f"{SERVER_URL}{path}", headers=_AUTH_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        print(f"[engine] Server GET {path} → {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    except requests.RequestException as e:
        print(f"[engine] Server GET {path} error: {e}", file=sys.stderr)
        return None

def _server_post(path, body, timeout=30):
    try:
        r = requests.post(f"{SERVER_URL}{path}", json=body, headers=_AUTH_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        print(f"[engine] Server POST {path} → {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    except requests.RequestException as e:
        print(f"[engine] Server POST {path} error: {e}", file=sys.stderr)
        return None

# ────────────────────  Data persistence  ────────────────────

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

# ────────────────────  Character class  ─────────────────────

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

    def get_last_reset_boundary(self, reset_type: str) -> datetime:
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

    def toggle_activity(self, activity_name: str):
        if activity_name in self.activities:
            cur = self.activities[activity_name]["status"]
            self.activities[activity_name]["status"] = "completed" if cur == "available" else "available"

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
        char.equipment            = d.get("equipment", [])
        char.equipment_last_check = datetime.fromisoformat(d["equipment_last_check"]) if d.get("equipment_last_check") else None
        char.activities           = d["activities"]
        char.last_reset_check     = datetime.fromisoformat(
            d.get("last_reset_check", datetime.now(UTC).isoformat()))
        return char

# ────────────────────  Helpers  ─────────────────────────────

def find_character(characters, name, realm):
    n, r = name.lower(), realm.lower()
    return next((c for c in characters if c.name.lower() == n and c.realm.lower() == r), None)

def _char_from_server(data):
    return Character(
        data.get("name", ""),
        data.get("level", "?"),
        data.get("realm", ""),
        data.get("region", "eu"),
        portrait_url = data.get("portrait_url"),
        avatar_url   = data.get("avatar_url"),
        class_id     = data.get("class_id"),
        class_name   = data.get("class_name", ""),
        spec_name    = data.get("spec_name", ""),
        class_slug   = data.get("class_slug", ""),
        spec_slug    = data.get("spec_slug", ""),
        item_level   = data.get("item_level", 0),
    )

# ────────────────────  Main loop  ───────────────────────────

def main():
    emit({"status": "ready"})
    emit({"status": "connecting"})

    try:
        h = requests.get(f"{SERVER_URL}/health", timeout=30)
        if h.status_code == 200:
            emit({"status": "connected"})
        else:
            print(f"[engine] Server health check failed: {h.status_code}", file=sys.stderr)
            emit({"status": "connect_failed"})
    except Exception as e:
        print(f"[engine] Server unreachable ({e}) — online features may fail", file=sys.stderr)
        emit({"status": "connect_failed"})

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
            data = _server_get(f"/realms/{region}")
            realms = data.get("realms", []) if data else []
            emit({"status": "realms", "region": region, "realms": realms})

        elif command.startswith("ADD_CHARACTER:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                data = _server_get(f"/character/{region}/{realm}/{name}")
                if data:
                    char = _char_from_server(data)
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
                data = _server_post("/auto-add", {"region": region, "name": name}, timeout=120)
                if data:
                    char = _char_from_server(data)
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

                items = _server_get(f"/equipment/{region}/{realm}/{name}")
                if items is not None:
                    if char and items:
                        char.equipment = items
                        char.equipment_last_check = datetime.now(UTC)
                        save_data(characters)
                    emit({"status": "equipment", "name": name, "realm": realm,
                          "items": items if items else [], "cached": False})
                else:
                    emit({"status": "error", "message": "Could not fetch equipment from server"})

        elif command.startswith("REFRESH_EQUIPMENT:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                emit({"status": "equipment_refreshing", "name": name, "realm": realm})
                items = _server_get(f"/equipment/{region}/{realm}/{name}")
                if items is not None:
                    if char:
                        char.equipment = items if items else []
                        char.equipment_last_check = datetime.now(UTC)
                        save_data(characters)
                    emit({"status": "equipment", "name": name, "realm": realm,
                          "items": items if items else [], "cached": False})
                else:
                    emit({"status": "error", "message": "Could not fetch equipment from server"})

        elif command.startswith("FETCH_TALENT_TREE:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, class_slug, spec_slug = [p.strip() for p in parts]

                # Check local disk cache first
                cache_dir  = os.path.join(basedir, 'talent_tree_cache')
                cache_file = os.path.join(cache_dir, f'{class_slug}_{spec_slug}.json')
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r', encoding='utf-8') as f:
                            cached = json.load(f)
                        if cached.get('class_nodes') and cached.get('spec_nodes'):
                            print(f"[engine] Loaded cached talent tree: {cache_file}", file=sys.stderr)
                            emit({"status": "talent_tree", "class_slug": class_slug,
                                  "spec_slug": spec_slug, "tree": cached})
                            continue
                    except Exception as e:
                        print(f"[engine] Cache read error: {e}", file=sys.stderr)

                # Fetch from server
                try:
                    tree = _server_get(f"/talent-tree/{region}/{class_slug}/{spec_slug}", timeout=120)
                    if tree and tree.get('class_nodes'):
                        os.makedirs(cache_dir, exist_ok=True)
                        with open(cache_file, 'w', encoding='utf-8') as f:
                            json.dump(tree, f, indent=2, ensure_ascii=False)
                        print(f"[engine] Cached talent tree → {cache_file}", file=sys.stderr)
                        emit({"status": "talent_tree", "class_slug": class_slug,
                              "spec_slug": spec_slug, "tree": tree})
                    else:
                        emit({"status": "talent_tree_error",
                              "class_slug": class_slug, "spec_slug": spec_slug,
                              "message": f"Server returned no data for {class_slug}/{spec_slug}"})
                except Exception as e:
                    print(f"[engine] Talent tree fetch crashed: {e}", file=sys.stderr)
                    emit({"status": "talent_tree_error",
                          "class_slug": class_slug, "spec_slug": spec_slug,
                          "message": str(e)})

        elif command == "CLEAR_TALENT_CACHE":
            cache_dir = os.path.join(basedir, 'talent_tree_cache')
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
            emit({"status": "success", "message": "Talent tree cache cleared"})

        elif command == "EXIT":
            save_data(characters)
            break


if __name__ == "__main__":
    main()
