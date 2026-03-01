# Innkeeper - Version 1.0
# @Author: eightmouse

# ------------[      MODULES      ]------------ #
import json, requests, os, sys, shutil, unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as _urlquote

UTC     = timezone.utc
if getattr(sys, 'frozen', False):
    basedir = os.path.abspath(os.path.dirname(sys.executable))
else:
    basedir = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))

SERVER_URL = "https://innkeper.onrender.com"
AUTH_KEY   = "r7XkP9mQ2zW6vT4nY8sH3dFa1cJuE5LbG0tC" # App-level routing key, NOT a secret. Server uses env vars for Blizzard OAuth. Rate-limited per IP.
DATA_FILE  = os.path.join(basedir, 'characters.json')

# ── Shared constants (used by both server and client) ──
KP_SOURCES = ["Weekly Quest", "Treatise", "Moxie Order", "Field Notes", "Weekly Treasures", "Notebook"]

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
    AUTH_KEY             = os.getenv("AUTH_KEY", "")

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

    # ────────────────────  Rate limiter  ────────────────────────

    import collections

    _rate_buckets: dict[str, collections.deque] = {}   # ip → deque of timestamps
    _RATE_GENERAL    = (30, 60)   # 30 requests per 60 seconds
    _RATE_HEAVY      = (5, 60)    # 5 requests per 60 seconds  (auto-add, decor)
    _HEAVY_PREFIXES  = ("/auto-add", "/decor/")
    _rate_state = {"last_clean": 0.0}

    def _rate_key(ip: str, path: str) -> str:
        for prefix in _HEAVY_PREFIXES:
            if path.startswith(prefix):
                return f"{ip}::heavy"
        return f"{ip}::gen"

    def _rate_check(key: str, path: str) -> bool:
        """Return True if the request is allowed."""
        now = time.time()
        is_heavy = any(path.startswith(p) for p in _HEAVY_PREFIXES)
        max_req, window = _RATE_HEAVY if is_heavy else _RATE_GENERAL

        bucket = _rate_buckets.setdefault(key, collections.deque())
        # Evict old entries
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= max_req:
            return False
        bucket.append(now)
        return True

    def _rate_cleanup():
        """Periodically remove stale IPs (every 5 min)."""
        now = time.time()
        if now - _rate_state["last_clean"] < 300:
            return
        _rate_state["last_clean"] = now
        stale = [k for k, v in _rate_buckets.items() if not v or v[-1] < now - 120]
        for k in stale:
            del _rate_buckets[k]

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        key = _rate_key(ip, request.url.path)
        _rate_cleanup()
        if not _rate_check(key, request.url.path):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Try again shortly."})
        return await call_next(request)

    # ────────────────────  Token Management  ────────────────────

    _token_cache: dict[str, dict] = {}
    TOKEN_TTL = 24 * 3600 - 300

    # TW OAuth redirects to a dead apac host; use KR's endpoint instead (same APAC token)
    _OAUTH_HOST = {"tw": "kr"}

    def get_access_token(region: str = "eu") -> str | None:
        cached = _token_cache.get(region)
        if cached and cached["expires"] > time.time():
            return cached["token"]
        oauth_host = _OAUTH_HOST.get(region, region)
        r = requests.post(
            f"https://{oauth_host}.battle.net/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET),
            timeout=10,
        )
        if r.status_code == 200:
            token = r.json()["access_token"]
            _token_cache[region] = {"token": token, "expires": time.time() + TOKEN_TTL}
            return token
        print(f"[engine] OAuth failed for region={region} (host={oauth_host}): {r.status_code} {r.text[:200]}", flush=True)
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
        r = unicodedata.normalize("NFC", realm).lower()
        slug = r.replace(" ", "-").replace("'", "").replace("\u2019", "").replace(".", "")
        return _urlquote(slug, safe="-")

    def _api_name(name):
        """Normalize + lowercase + URL-encode a character name for Blizzard API paths."""
        return _urlquote(unicodedata.normalize("NFC", name).lower(), safe="")

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

    # ────────────────────  Vault constants  ─────────────────────

    # Midnight Season 1: keystone level → Great Vault reward ilvl
    VAULT_KEYSTONE_ILVL = {
        2:  259, 3:  259,
        4:  263, 5:  263,
        6:  266,
        7:  269, 8:  269, 9:  269,
        10: 272,
    }
    VAULT_KEYSTONE_MAX = 10  # keys above this still give max reward (Myth 1)

    VAULT_RAID_DIFFICULTY_ILVL = {
        'LFR': 243, 'Normal': 256, 'Heroic': 269, 'Mythic': 282,
    }
    VAULT_RAID_DIFFICULTY_SHORT = {
        'LFR': 'L', 'Normal': 'N', 'Heroic': 'H', 'Mythic': 'M',
    }

    # Midnight Season 1: world activity tier → Great Vault reward ilvl
    VAULT_WORLD_TIER_ILVL = {
        1: 239, 2: 239, 3: 242, 4: 246,
        5: 249, 6: 252, 7: 255, 8: 259,
        9: 262, 10: 265, 11: 268,
    }

    # ────────────────────  Profession constants  ────────────────

    CONC_MAX = 1000
    CONC_REGEN_SECONDS = 4 * 86400  # 4 days to full

    PROFESSION_ICONS = {
        171: "trade_alchemy", 164: "trade_blacksmithing",
        333: "trade_engraving", 202: "trade_engineering",
        182: "trade_herbalism", 773: "inv_inscription_tradeskill01",
        755: "inv_misc_gem_01", 165: "trade_leatherworking",
        186: "trade_mining", 393: "inv_misc_pelt_wolf_01",
        197: "trade_tailoring", 185: "inv_misc_food_15",
        356: "trade_fishing",
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
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{_api_name(name)}",
            _params(region), token)

    def _fetch_character_media(region, realm, name, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{_api_name(name)}/character-media",
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
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{_api_name(name)}/equipment",
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

    # ────────────────────  Vault data helpers  ──────────────────

    def _fetch_mythic_keystone_profile(region, realm, name, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{_api_name(name)}/mythic-keystone-profile",
            _params(region), token)
        if not data:
            return {"best_runs": [], "vault_rewards": {}, "keystone_ilvl_map": VAULT_KEYSTONE_ILVL}

        current = data.get("current_period", {})
        raw_runs = current.get("best_runs", [])
        # Sort descending by keystone level
        raw_runs.sort(key=lambda r: r.get("keystone_level", 0), reverse=True)

        best_runs = []
        for run in raw_runs[:8]:
            best_runs.append({
                "keystone_level":           run.get("keystone_level", 0),
                "dungeon":                  run.get("dungeon", {}).get("name", "Unknown"),
                "is_completed_within_time": run.get("is_completed_within_time", False),
                "duration":                 run.get("duration", 0),
            })

        # Compute vault rewards for milestone positions (run indices 0, 3, 7)
        vault_rewards = {}
        for slot_idx, run_idx in enumerate([0, 3, 7]):
            slot_key = str(slot_idx + 1)
            if run_idx < len(best_runs):
                level = best_runs[run_idx]["keystone_level"]
                capped = min(level, VAULT_KEYSTONE_MAX)
                ilvl = VAULT_KEYSTONE_ILVL.get(capped, VAULT_KEYSTONE_ILVL.get(VAULT_KEYSTONE_MAX, 625))
                vault_rewards[slot_key] = {
                    "ilvl": ilvl,
                    "from_level": level,
                    "unlocked": True,
                }
            else:
                vault_rewards[slot_key] = {"ilvl": 0, "from_level": 0, "unlocked": False}

        return {
            "best_runs":       best_runs,
            "vault_rewards":   vault_rewards,
            "keystone_ilvl_map": VAULT_KEYSTONE_ILVL,
        }

    def _fetch_raid_encounters(region, realm, name, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{_api_name(name)}/encounters/raids",
            _params(region), token)
        if not data:
            return {"kills_this_week": [], "total_kills": 0, "difficulty_breakdown": {}}

        # Calculate weekly reset boundary (Wednesday 8 AM UTC)
        now = datetime.now(UTC)
        boundary = now.replace(hour=8, minute=0, second=0, microsecond=0)
        days_since_wed = (now.weekday() - 2) % 7
        boundary -= timedelta(days=days_since_wed)
        if now < boundary:
            boundary -= timedelta(days=7)
        reset_ts = int(boundary.timestamp() * 1000)  # Blizzard uses milliseconds

        kills_this_week = []
        seen_bosses = {}  # boss_name → highest difficulty kill

        # Filter to latest expansion — iterate all expansions/instances
        expansions = data.get("expansions", [])
        if expansions:
            latest_exp = expansions[-1]  # Last expansion is latest
            for instance in latest_exp.get("instances", []):
                raid_name = instance.get("instance", {}).get("name", "Unknown Raid")
                for mode in instance.get("modes", []):
                    difficulty = mode.get("difficulty", {}).get("name", "Normal")
                    for enc in mode.get("encounters", []):
                        last_kill = enc.get("last_kill_timestamp", 0)
                        if last_kill >= reset_ts:
                            boss_name = enc.get("encounter", {}).get("name", "Unknown Boss")
                            # Rank difficulties for deduplication
                            diff_rank = {"LFR": 0, "Normal": 1, "Heroic": 2, "Mythic": 3}.get(difficulty, 1)
                            if boss_name not in seen_bosses or diff_rank > seen_bosses[boss_name]["rank"]:
                                seen_bosses[boss_name] = {
                                    "boss":       boss_name,
                                    "raid":       raid_name,
                                    "difficulty": difficulty,
                                    "rank":       diff_rank,
                                }

        kills_this_week = sorted(seen_bosses.values(), key=lambda k: k["rank"], reverse=True)
        # Remove internal rank field
        for k in kills_this_week:
            k.pop("rank", None)

        # Difficulty breakdown
        diff_breakdown = {}
        for k in kills_this_week:
            diff = k["difficulty"]
            diff_breakdown[diff] = diff_breakdown.get(diff, 0) + 1

        return {
            "kills_this_week":      kills_this_week,
            "total_kills":          len(kills_this_week),
            "difficulty_breakdown": diff_breakdown,
        }

    # ────────────────────  Profession data helpers  ─────────────

    def _fetch_professions(region, realm, name, token):
        data = _blizzard_get(
            f"https://{region}.api.blizzard.com/profile/wow/character/{_slug(realm)}/{_api_name(name)}/professions",
            _params(region), token)
        if not data:
            return {"primaries": []}

        primaries = []
        for prof in data.get("primaries", []):
            pid = prof.get("profession", {}).get("id")
            pname = prof.get("profession", {}).get("name", "Unknown")
            icon_key = PROFESSION_ICONS.get(pid, "trade_alchemy")
            icon_url = f"https://render.worldofwarcraft.com/us/icons/56/{icon_key}.jpg"

            # Find Midnight-tier skill (latest expansion tier)
            skill = 0
            max_skill = 0
            for tier in prof.get("tiers", []):
                # Use the last tier entry (most recent expansion)
                skill = tier.get("skill_points", 0)
                max_skill = tier.get("max_skill_points", 0)

            primaries.append({
                "id": pid,
                "name": pname,
                "icon_url": icon_url,
                "skill": skill,
                "max_skill": max_skill,
            })

        return {"primaries": primaries}

    # ────────────────────  Housing decor helpers  ──────────────────

    def _fetch_decor_index(region, token):
        """Fetch the housing decor index from Blizzard's static API."""
        url = f"https://{region}.api.blizzard.com/data/wow/decor/index"
        params = _params(region, namespace_prefix="static")
        print(f"[engine] Fetching decor index: {url} params={params}", file=sys.stderr, flush=True)
        try:
            r = requests.get(url, params=params,
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
            print(f"[engine] Decor index response: {r.status_code} (len={len(r.content)})", file=sys.stderr, flush=True)
            if r.status_code == 200:
                data = r.json()
                print(f"[engine] Decor index keys: {list(data.keys())}", file=sys.stderr, flush=True)
                return data
            else:
                print(f"[engine] Decor index error body: {r.text[:500]}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[engine] Decor index fetch exception: {e}", file=sys.stderr, flush=True)
        return None

    def _fetch_decor_detail(region, decor_id, token):
        """Fetch detail for a single decor item (used if index lacks category/source)."""
        return _blizzard_get(
            f"https://{region}.api.blizzard.com/data/wow/decor/{decor_id}",
            _params(region, namespace_prefix="static"), token, timeout=10)

    def _normalize_decor_item(raw_item, detail=None):
        """Extract id, name, category, source, icon_url from a raw API item."""
        merged = {**(detail or {}), **(raw_item or {})}

        item_id = merged.get("id", 0)

        # Name
        name_raw = merged.get("name", "")
        name = name_raw if isinstance(name_raw, str) else str(name_raw)

        # Category — probe multiple keys
        category = None
        for key in ("category", "decor_category", "type"):
            val = merged.get(key)
            if val:
                category = val.get("name", str(val)) if isinstance(val, dict) else str(val)
                break
        if not category:
            category = "Uncategorized"

        # Source — probe multiple keys
        source = None
        for key in ("source", "acquisition", "description"):
            val = merged.get(key)
            if val:
                source = val.get("name", str(val)) if isinstance(val, dict) else str(val)
                break

        # Icon — extract from media.assets where key == "icon"
        icon_url = None
        media = merged.get("media")
        if isinstance(media, dict):
            for asset in media.get("assets", []):
                if isinstance(asset, dict) and asset.get("key") == "icon":
                    icon_url = asset.get("value")
                    break
            # Also try key.href for media reference
            if not icon_url:
                key_ref = media.get("key", {})
                if isinstance(key_ref, dict):
                    href = key_ref.get("href", "")
                    if href:
                        icon_url = href  # Will be resolved later if needed

        return {
            "id": item_id,
            "name": name,
            "category": category,
            "source": source or "",
            "icon_url": icon_url,
        }

    def _build_decor_catalog(region, token):
        """Fetch the full decor catalog, enrich with icons from Blizzard item media API."""
        raw = _fetch_decor_index(region, token)
        if not raw:
            return None

        # Probe for the items list under various possible keys
        items_list = None
        for key in ("decor_items", "decors", "decorations", "items", "results"):
            if key in raw and isinstance(raw[key], list):
                items_list = raw[key]
                print(f"[engine] Decor items found under key '{key}': {len(items_list)} items", file=sys.stderr, flush=True)
                break

        if items_list is None:
            if isinstance(raw, list):
                items_list = raw
            else:
                print(f"[engine] Could not find items list in decor index. Keys: {list(raw.keys())}", file=sys.stderr, flush=True)
                return None

        normalized = [_normalize_decor_item(it) for it in items_list if isinstance(it, dict)]

        # --- Enrich: decor detail → item detail + media (single pass per item) ---
        def _enrich_decor(decor_id):
            """Fetch decor detail → WoW item detail + media in one worker."""
            result = {"icon_url": None, "description": "", "category": ""}
            try:
                # 1) Decor detail → get linked WoW item ID
                detail = _fetch_decor_detail(region, decor_id, token)
                if not detail or not isinstance(detail.get("items"), dict):
                    return (decor_id, result)
                item_id = detail["items"].get("id")
                if not item_id:
                    return (decor_id, result)

                # 2) Item detail → description + category
                item_data = _blizzard_get(
                    f"https://{region}.api.blizzard.com/data/wow/item/{item_id}",
                    _params(region, namespace_prefix="static"), token, timeout=10)
                if item_data:
                    desc = item_data.get("description", "")
                    result["description"] = desc if isinstance(desc, str) else str(desc) if desc else ""
                    sub = item_data.get("item_subclass")
                    if isinstance(sub, dict) and sub.get("name"):
                        result["category"] = sub["name"]

                # 3) Item media → icon URL
                media = _blizzard_get(
                    f"https://{region}.api.blizzard.com/data/wow/media/item/{item_id}",
                    _params(region, namespace_prefix="static"), token, timeout=10)
                if media:
                    for asset in media.get("assets", []):
                        if isinstance(asset, dict) and asset.get("key") == "icon":
                            result["icon_url"] = asset.get("value")
                            break
            except Exception:
                pass
            return (decor_id, result)

        decor_ids = [it["id"] for it in normalized if it.get("id")]
        enrichment = {}
        print(f"[engine] Enriching {len(decor_ids)} decor items (detail + item + media)...", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(max_workers=30) as pool:
            for decor_id, info in pool.map(_enrich_decor, decor_ids):
                enrichment[decor_id] = info
        icons = sum(1 for v in enrichment.values() if v["icon_url"])
        descs = sum(1 for v in enrichment.values() if v["description"])
        cats  = sum(1 for v in enrichment.values() if v["category"])
        print(f"[engine] Enrichment done: {icons} icons, {descs} descriptions, {cats} categories out of {len(decor_ids)}", file=sys.stderr, flush=True)

        # Merge enrichment into normalized items
        for item in normalized:
            info = enrichment.get(item["id"], {})
            if info.get("icon_url"):
                item["icon_url"] = info["icon_url"]
            if info.get("description"):
                item["source"] = info["description"]
            if info.get("category") and item.get("category") == "Uncategorized":
                item["category"] = info["category"]

        # Extract unique categories
        categories = ["All"]
        seen_cats = set()
        for it in normalized:
            cat = it["category"]
            if cat and cat not in seen_cats:
                seen_cats.add(cat)
                categories.append(cat)

        return {
            "items": normalized,
            "categories": categories,
            "fetched_at": datetime.now(UTC).isoformat(),
            "region": region,
        }

    def _concentration_time_to_full(current, updated_at_iso):
        """Returns seconds until concentration is full, accounting for regen since update."""
        if not updated_at_iso:
            return 0
        updated_at = datetime.fromisoformat(updated_at_iso)
        elapsed = (datetime.now(UTC) - updated_at).total_seconds()
        rate = CONC_MAX / CONC_REGEN_SECONDS
        effective = min(current + elapsed * rate, CONC_MAX)
        if effective >= CONC_MAX:
            return 0
        return (CONC_MAX - effective) / rate

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
    def realms(region: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return {"region": region, "realms": _fetch_realms(region, token)}

    @app.get("/character/{region}/{realm}/{name}")
    def character(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        data = _fetch_character(region, realm, name, token)
        if not data:
            raise HTTPException(404, f"Character {name} not found on {realm}-{region}")
        return _build_character_dict(data, region, realm, name, token)

    @app.get("/equipment/{region}/{realm}/{name}")
    def equipment(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return _fetch_equipment(region, realm, name, token)

    @app.get("/talent-tree/{region}/{class_slug}/{spec_slug}")
    def talent_tree(region: str, class_slug: str, spec_slug: str):
        try:
            return _fetch_talent_tree_from_blizzard(region, class_slug, spec_slug)
        except (ValueError, ConnectionError) as e:
            raise HTTPException(400, str(e))

    @app.get("/vault/mythic-plus/{region}/{realm}/{name}")
    def vault_mythic_plus(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return _fetch_mythic_keystone_profile(region, realm, name, token)

    @app.get("/vault/raids/{region}/{realm}/{name}")
    def vault_raids(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return _fetch_raid_encounters(region, realm, name, token)

    @app.get("/professions/{region}/{realm}/{name}")
    def professions(region: str, realm: str, name: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        return _fetch_professions(region, realm, name, token)

    @app.get("/decor/index/{region}")
    def decor_index(region: str):
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        catalog = _build_decor_catalog(region, token)
        if not catalog:
            raise HTTPException(502, "Could not fetch decor catalog from Blizzard API")
        return catalog

    @app.get("/decor/debug/{region}")
    def decor_debug(region: str):
        """Temporary debug endpoint — returns raw Blizzard response for decor index."""
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        url = f"https://{region}.api.blizzard.com/data/wow/decor/index"
        params = _params(region, namespace_prefix="static")
        try:
            r = requests.get(url, params=params,
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
            return {"status_code": r.status_code, "body_preview": r.text[:2000],
                    "url": url, "params": params}
        except Exception as e:
            return {"error": str(e), "url": url, "params": params}

    @app.get("/decor/debug/{region}/{decor_id}")
    def decor_debug_detail(region: str, decor_id: int):
        """Temporary debug endpoint — returns raw Blizzard response for a single decor item."""
        token = get_access_token(region)
        if not token:
            raise HTTPException(502, "Failed to get Blizzard API token")
        url = f"https://{region}.api.blizzard.com/data/wow/decor/{decor_id}"
        params = _params(region, namespace_prefix="static")
        try:
            r = requests.get(url, params=params,
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
            return {"status_code": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:1000],
                    "url": url, "params": params}
        except Exception as e:
            return {"error": str(e), "url": url, "params": params}

    @app.post("/auto-add")
    def auto_add(body: dict):
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
    import time as _time
    for attempt in range(2):
        try:
            r = requests.get(f"{SERVER_URL}{path}", headers=_AUTH_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403 and attempt == 0:
                print(f"[engine] Server GET {path} → 403, retrying in 3s…", file=sys.stderr)
                _time.sleep(3)
                continue
            print(f"[engine] Server GET {path} → {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[engine] Server GET {path} error: {e}", file=sys.stderr)
        break
    return None

def _server_post(path, body, timeout=30):
    import time as _time
    for attempt in range(2):
        try:
            r = requests.post(f"{SERVER_URL}{path}", json=body, headers=_AUTH_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403 and attempt == 0:
                print(f"[engine] Server POST {path} → 403, retrying in 3s…", file=sys.stderr)
                _time.sleep(3)
                continue
            print(f"[engine] Server POST {path} → {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[engine] Server POST {path} error: {e}", file=sys.stderr)
        break
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
        self.vault_mythic_plus = {}
        self.vault_raids = {}
        self.vault_world = [None] * 8
        self.vault_last_check = None
        self.vault_world_last_reset = None
        self.professions          = {}
        self.professions_last_check = None
        self.prof_moxie           = {}
        self.prof_concentration   = {}
        self.prof_spark           = False
        self.prof_spark_collected_at = None
        self.prof_kp              = {}
        self.prof_kp_last_reset   = None
        self.housing_tracked      = {}
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
        # Reset vault world toggles on weekly boundary
        last_world = self.vault_world_last_reset or datetime.min.replace(tzinfo=UTC)
        if last_world < weekly_b:
            self.vault_world = [None] * 8
            self.vault_mythic_plus = {}
            self.vault_raids = {}
            self.vault_last_check = None
            self.vault_world_last_reset = datetime.now(UTC)
            modified = True
        # Reset profession weekly tracking on weekly boundary
        last_kp = self.prof_kp_last_reset or datetime.min.replace(tzinfo=UTC)
        if last_kp < weekly_b:
            self.prof_spark = False
            self.prof_spark_collected_at = None
            for prof_name in self.prof_kp:
                for src in self.prof_kp[prof_name]:
                    self.prof_kp[prof_name][src] = False
            self.prof_kp_last_reset = datetime.now(UTC)
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
            "vault_mythic_plus":     self.vault_mythic_plus,
            "vault_raids":           self.vault_raids,
            "vault_world":           self.vault_world,
            "vault_last_check":      self.vault_last_check.isoformat() if self.vault_last_check else None,
            "vault_world_last_reset": self.vault_world_last_reset.isoformat() if self.vault_world_last_reset else None,
            "professions":           self.professions,
            "professions_last_check": self.professions_last_check.isoformat() if self.professions_last_check else None,
            "prof_moxie":            self.prof_moxie,
            "prof_concentration":    self.prof_concentration,
            "prof_spark":            self.prof_spark,
            "prof_spark_collected_at": self.prof_spark_collected_at,
            "prof_kp":               self.prof_kp,
            "prof_kp_last_reset":    self.prof_kp_last_reset.isoformat() if self.prof_kp_last_reset else None,
            "housing_tracked":       self.housing_tracked,
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
        char.vault_mythic_plus    = d.get("vault_mythic_plus", {})
        char.vault_raids          = d.get("vault_raids", {})
        raw_world = d.get("vault_world", [None] * 8)
        char.vault_world          = [v if isinstance(v, dict) else None for v in raw_world]
        char.vault_last_check     = datetime.fromisoformat(d["vault_last_check"]) if d.get("vault_last_check") else None
        char.vault_world_last_reset = datetime.fromisoformat(d["vault_world_last_reset"]) if d.get("vault_world_last_reset") else None
        char.professions            = d.get("professions", {})
        char.professions_last_check = datetime.fromisoformat(d["professions_last_check"]) if d.get("professions_last_check") else None
        char.prof_moxie             = d.get("prof_moxie", {})
        char.prof_concentration     = d.get("prof_concentration", {})
        char.prof_spark             = d.get("prof_spark", False)
        char.prof_spark_collected_at = d.get("prof_spark_collected_at", None)
        char.prof_kp                = d.get("prof_kp", {})
        char.prof_kp_last_reset     = datetime.fromisoformat(d["prof_kp_last_reset"]) if d.get("prof_kp_last_reset") else None
        char.housing_tracked        = d.get("housing_tracked", {})
        char.activities           = d["activities"]
        char.last_reset_check     = datetime.fromisoformat(
            d.get("last_reset_check", datetime.now(UTC).isoformat()))
        return char

# ────────────────────  Helpers  ─────────────────────────────

def find_character(characters, name, realm):
    n = unicodedata.normalize("NFC", name).lower()
    r = unicodedata.normalize("NFC", realm).lower()
    return next((c for c in characters if
                 unicodedata.normalize("NFC", c.name).lower() == n and
                 unicodedata.normalize("NFC", c.realm).lower() == r), None)

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
    # Force UTF-8 for IPC with Electron (Windows defaults to cp1252)
    if sys.platform == "win32":
        sys.stdin  = open(sys.stdin.fileno(),  mode='r', encoding='utf-8', errors='replace', closefd=False)
        sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
        sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)

    global basedir, DATA_FILE
    if '--datadir' in sys.argv:
        idx = sys.argv.index('--datadir')
        if idx + 1 < len(sys.argv):
            basedir = sys.argv[idx + 1]
            DATA_FILE = os.path.join(basedir, 'characters.json')

    emit({"status": "ready"})

    characters = load_data()
    for char in characters:
        char.check_resets()
        # Clear stale vault cache so fresh data uses current ilvl tables
        char.vault_mythic_plus = {}
        char.vault_raids = {}
        char.vault_last_check = None
    save_data(characters)

    import time
    server_checked = False

    for raw_line in sys.stdin:
        command = raw_line.strip()
        if not command:
            continue

        if not server_checked:
            server_checked = True
            emit({"status": "connecting"})
            connected = False
            for attempt in range(3):
                try:
                    h = requests.get(f"{SERVER_URL}/health", timeout=30)
                    if h.status_code == 200:
                        emit({"status": "connected"})
                        connected = True
                        break
                    print(f"[engine] Health check attempt {attempt+1}/3 failed: {h.status_code}", file=sys.stderr)
                except Exception as e:
                    print(f"[engine] Health check attempt {attempt+1}/3 error: {e}", file=sys.stderr)
                if attempt < 2:
                    time.sleep(5)
            if not connected:
                emit({"status": "connect_failed"})

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
                data = _server_get(f"/character/{region}/{_urlquote(realm, safe='')}/{_urlquote(name, safe='')}")
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

                items = _server_get(f"/equipment/{region}/{_urlquote(realm, safe='')}/{_urlquote(name, safe='')}")
                if items is not None:
                    if char and items:
                        char.equipment = items
                        char.equipment_last_check = datetime.now(UTC)
                        save_data(characters)
                    emit({"status": "equipment", "name": name, "realm": realm,
                          "items": items if items else [], "cached": False})
                else:
                    emit({"status": "equipment_error", "name": name, "realm": realm,
                          "message": "Could not fetch equipment from server"})

        elif command.startswith("REFRESH_EQUIPMENT:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                emit({"status": "equipment_refreshing", "name": name, "realm": realm})
                items = _server_get(f"/equipment/{region}/{_urlquote(realm, safe='')}/{_urlquote(name, safe='')}")
                if items is not None:
                    if char:
                        char.equipment = items if items else []
                        char.equipment_last_check = datetime.now(UTC)
                        save_data(characters)
                    emit({"status": "equipment", "name": name, "realm": realm,
                          "items": items if items else [], "cached": False})
                else:
                    emit({"status": "equipment_error", "name": name, "realm": realm,
                          "message": "Could not fetch equipment from server"})

        elif command.startswith("REFRESH_SPEC:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if not char:
                    emit({"status": "spec_refresh_error",
                          "message": f"Character {name} on {realm} not found locally"})
                    continue
                data = _server_get(f"/character/{region}/{realm}/{name}")
                if data:
                    old_spec = char.spec_slug
                    char.spec_name    = data.get("spec_name", char.spec_name)
                    char.spec_slug    = data.get("spec_slug", char.spec_slug)
                    char.item_level   = data.get("item_level", char.item_level)
                    char.portrait_url = data.get("portrait_url", char.portrait_url)
                    char.avatar_url   = data.get("avatar_url", char.avatar_url)
                    spec_changed = (old_spec != char.spec_slug)
                    if spec_changed:
                        save_data(characters)
                    emit({"status": "spec_refreshed", "name": name, "realm": realm,
                          "character": char.to_dict(), "spec_changed": spec_changed})
                else:
                    emit({"status": "spec_refresh_error",
                          "message": "Could not fetch character data from server"})

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

        elif command.startswith("GET_VAULT:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)

                if char and char.vault_last_check:
                    cache_age = (datetime.now(UTC) - char.vault_last_check).total_seconds()
                    if cache_age < 300:
                        emit({"status": "vault_data", "name": name, "realm": realm,
                              "mythic_plus": char.vault_mythic_plus,
                              "raids": char.vault_raids,
                              "world": char.vault_world, "cached": True})
                        continue

                _r, _n = _urlquote(realm, safe=''), _urlquote(name, safe='')
                mp_data = _server_get(f"/vault/mythic-plus/{region}/{_r}/{_n}")
                raid_data = _server_get(f"/vault/raids/{region}/{_r}/{_n}")
                if mp_data is None:
                    mp_data = {"best_runs": [], "vault_rewards": {}, "keystone_ilvl_map": {}}
                if raid_data is None:
                    raid_data = {"kills_this_week": [], "total_kills": 0, "difficulty_breakdown": {}}

                if char:
                    char.vault_mythic_plus = mp_data
                    char.vault_raids = raid_data
                    char.vault_last_check = datetime.now(UTC)
                    save_data(characters)

                emit({"status": "vault_data", "name": name, "realm": realm,
                      "mythic_plus": mp_data, "raids": raid_data,
                      "world": char.vault_world if char else [None] * 8,
                      "cached": False})

        elif command.startswith("SET_VAULT_WORLD:"):
            parts = command.split(":", 5)
            if len(parts) == 6:
                _, name, realm, slot_str, wtype, tier_str = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    slot = int(slot_str)
                    tier = int(tier_str)
                    if 0 <= slot < len(char.vault_world) and 1 <= tier <= 11:
                        char.vault_world[slot] = {"type": wtype, "tier": tier}
                        save_data(characters)
                        emit({"status": "vault_world_toggled", "name": name,
                              "realm": realm, "world": char.vault_world})

        elif command.startswith("CLEAR_VAULT_WORLD:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, name, realm, slot_str = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    slot = int(slot_str)
                    if 0 <= slot < len(char.vault_world):
                        char.vault_world[slot] = None
                        save_data(characters)
                        emit({"status": "vault_world_toggled", "name": name,
                              "realm": realm, "world": char.vault_world})

        elif command.startswith("REFRESH_VAULT:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                emit({"status": "vault_refreshing", "name": name, "realm": realm})

                _r, _n = _urlquote(realm, safe=''), _urlquote(name, safe='')
                mp_data = _server_get(f"/vault/mythic-plus/{region}/{_r}/{_n}")
                raid_data = _server_get(f"/vault/raids/{region}/{_r}/{_n}")
                if mp_data is None:
                    mp_data = {"best_runs": [], "vault_rewards": {}, "keystone_ilvl_map": {}}
                if raid_data is None:
                    raid_data = {"kills_this_week": [], "total_kills": 0, "difficulty_breakdown": {}}

                if char:
                    char.vault_mythic_plus = mp_data
                    char.vault_raids = raid_data
                    char.vault_last_check = datetime.now(UTC)
                    save_data(characters)

                emit({"status": "vault_data", "name": name, "realm": realm,
                      "mythic_plus": mp_data, "raids": raid_data,
                      "world": char.vault_world if char else [None] * 8,
                      "cached": False})

        elif command.startswith("GET_PROFESSIONS:"):
            parts = command.split(":", 3)
            if len(parts) == 4:
                _, region, realm, name = [p.strip() for p in parts]
                char = find_character(characters, name, realm)

                if char and char.professions_last_check:
                    cache_age = (datetime.now(UTC) - char.professions_last_check).total_seconds()
                    if cache_age < 300:
                        emit({"status": "professions_data", "name": name, "realm": realm,
                              "professions": char.professions,
                              "moxie": char.prof_moxie,
                              "concentration": char.prof_concentration,
                              "spark": char.prof_spark,
                              "spark_collected_at": char.prof_spark_collected_at,
                              "kp": char.prof_kp, "cached": True})
                        continue

                _r, _n = _urlquote(realm, safe=''), _urlquote(name, safe='')
                prof_data = _server_get(f"/professions/{region}/{_r}/{_n}")
                if prof_data is None:
                    prof_data = {"primaries": []}

                if char:
                    char.professions = prof_data
                    char.professions_last_check = datetime.now(UTC)
                    save_data(characters)

                emit({"status": "professions_data", "name": name, "realm": realm,
                      "professions": prof_data,
                      "moxie": char.prof_moxie if char else {},
                      "concentration": char.prof_concentration if char else {},
                      "spark": char.prof_spark if char else False,
                      "spark_collected_at": char.prof_spark_collected_at if char else None,
                      "kp": char.prof_kp if char else {},
                      "cached": False})

        elif command.startswith("SET_PROF_MOXIE:"):
            parts = command.split(":", 4)
            if len(parts) == 5:
                _, name, realm, profession, amount = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    char.prof_moxie[profession] = int(amount)
                    save_data(characters)
                    emit({"status": "prof_updated", "name": name, "realm": realm,
                          "moxie": char.prof_moxie,
                          "concentration": char.prof_concentration,
                          "spark": char.prof_spark,
                          "spark_collected_at": char.prof_spark_collected_at,
                          "kp": char.prof_kp})

        elif command.startswith("SET_PROF_CONCENTRATION:"):
            parts = command.split(":", 4)
            if len(parts) == 5:
                _, name, realm, profession, current = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    char.prof_concentration[profession] = {
                        "current": int(current),
                        "updated_at": datetime.now(UTC).isoformat()
                    }
                    save_data(characters)
                    emit({"status": "prof_updated", "name": name, "realm": realm,
                          "moxie": char.prof_moxie,
                          "concentration": char.prof_concentration,
                          "spark": char.prof_spark,
                          "spark_collected_at": char.prof_spark_collected_at,
                          "kp": char.prof_kp})

        elif command.startswith("TOGGLE_PROF_SPARK:"):
            parts = command.split(":", 2)
            if len(parts) == 3:
                _, name, realm = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    char.prof_spark = not char.prof_spark
                    if char.prof_spark:
                        char.prof_spark_collected_at = datetime.now(UTC).isoformat()
                    else:
                        char.prof_spark_collected_at = None
                    save_data(characters)
                    emit({"status": "prof_updated", "name": name, "realm": realm,
                          "moxie": char.prof_moxie,
                          "concentration": char.prof_concentration,
                          "spark": char.prof_spark,
                          "spark_collected_at": char.prof_spark_collected_at,
                          "kp": char.prof_kp})

        elif command.startswith("TOGGLE_PROF_KP:"):
            parts = command.split(":", 4)
            if len(parts) == 5:
                _, name, realm, profession, source = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    if profession not in char.prof_kp:
                        char.prof_kp[profession] = {s: False for s in KP_SOURCES}
                    char.prof_kp[profession][source] = not char.prof_kp[profession].get(source, False)
                    save_data(characters)
                    emit({"status": "prof_updated", "name": name, "realm": realm,
                          "moxie": char.prof_moxie,
                          "concentration": char.prof_concentration,
                          "spark": char.prof_spark,
                          "spark_collected_at": char.prof_spark_collected_at,
                          "kp": char.prof_kp})

        elif command.startswith("TRACK_HOUSING_ITEM:"):
            parts = command.split(":", 4)
            if len(parts) == 5:
                _, name, realm, item_id, material_keys_csv = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char:
                    if item_id in char.housing_tracked:
                        del char.housing_tracked[item_id]
                    else:
                        char.housing_tracked[item_id] = {mat: 0 for mat in material_keys_csv.split(",")}
                    save_data(characters)
                    emit({"status": "housing_updated", "name": name, "realm": realm,
                          "housing_tracked": char.housing_tracked})

        elif command.startswith("SET_HOUSING_MATERIAL:"):
            parts = command.split(":", 5)
            if len(parts) == 6:
                _, name, realm, item_id, material, amount = [p.strip() for p in parts]
                char = find_character(characters, name, realm)
                if char and item_id in char.housing_tracked:
                    char.housing_tracked[item_id][material] = int(amount)
                    save_data(characters)
                    emit({"status": "housing_updated", "name": name, "realm": realm,
                          "housing_tracked": char.housing_tracked})

        elif command.startswith("FETCH_HOUSING_CATALOG:"):
            region = command.split(":", 1)[1].strip()
            cache_dir  = os.path.join(basedir, 'housing_decor_cache')
            cache_file = os.path.join(cache_dir, 'decor_catalog.json')

            # Check disk cache (7-day TTL, must have icons)
            use_cache = False
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cached = json.load(f)
                    fetched_at = cached.get('fetched_at', '')
                    has_icons = any(it.get('icon_url') for it in (cached.get('items') or [])[:50])
                    if fetched_at and has_icons:
                        age = (datetime.now(UTC) - datetime.fromisoformat(fetched_at)).total_seconds()
                        if age < 7 * 86400:
                            print(f"[engine] Housing catalog cache hit (age={age/3600:.1f}h, icons=yes)", file=sys.stderr)
                            emit({"status": "housing_api_catalog", "catalog": cached})
                            use_cache = True
                    elif fetched_at and not has_icons:
                        print(f"[engine] Housing catalog cache stale (no icons), re-fetching", file=sys.stderr)
                except Exception as e:
                    print(f"[engine] Housing cache read error: {e}", file=sys.stderr)

            if not use_cache:
                try:
                    catalog = _server_get(f"/decor/index/{region}", timeout=300)
                    if catalog and catalog.get("items"):
                        os.makedirs(cache_dir, exist_ok=True)
                        with open(cache_file, 'w', encoding='utf-8') as f:
                            json.dump(catalog, f, ensure_ascii=False)
                        print(f"[engine] Housing catalog fetched & cached: {len(catalog['items'])} items", file=sys.stderr)
                        emit({"status": "housing_api_catalog", "catalog": catalog})
                    else:
                        emit({"status": "housing_api_catalog_error",
                              "message": "Server returned no decor data"})
                except Exception as e:
                    print(f"[engine] Housing catalog fetch error: {e}", file=sys.stderr)
                    emit({"status": "housing_api_catalog_error", "message": str(e)})

        elif command == "CLEAR_HOUSING_CACHE":
            cache_dir = os.path.join(basedir, 'housing_decor_cache')
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
            emit({"status": "success", "message": "Housing decor cache cleared"})

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
