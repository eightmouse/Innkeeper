# Innkeeper - Version 0.9
# @Author: eightmouse

# ------------[      MODULES      ]------------ #
import json, requests, os, sys
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

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
    Returns { 'main': <full armory scene URL>, 'avatar': <bust portrait URL> }.
    'main' is the class-background + character composite used by the Armory.
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
    # 'main' is the full armory scene (character + class background)
    for key in ("main", "main-raw"):
        if key in assets:
            result["main"] = assets[key]
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
    cls   = data.get("character_class", {})
    spec  = data.get("active_spec", {})
    media = fetch_character_media(region, realm, name, token)
    return Character(
        name, data.get("level", "?"), realm, region,
        portrait_url = media.get("main"),    # full armory scene (character + class bg)
        avatar_url   = media.get("avatar"),
        class_id     = cls.get("id"),
        class_name   = cls.get("name", ""),
        spec_name    = spec.get("name", ""),
    )

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
                 class_id=None, class_name="", spec_name=""):
        self.name         = name
        self.level        = level
        self.realm        = realm
        self.region       = region
        self.portrait_url = portrait_url   # full armory scene (class bg + character)
        self.avatar_url   = avatar_url     # small bust portrait
        self.class_id     = class_id
        self.class_name   = class_name
        self.spec_name    = spec_name
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
            "name":             self.name,
            "level":            self.level,
            "realm":            self.realm,
            "region":           self.region,
            "portrait_url":     self.portrait_url,
            "avatar_url":       self.avatar_url,
            "class_id":         self.class_id,
            "class_name":       self.class_name,
            "spec_name":        self.spec_name,
            "activities":       self.activities,
            "last_reset_check": self.last_reset_check.isoformat(),
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
        )
        char.activities       = d["activities"]
        char.last_reset_check = datetime.fromisoformat(
            d.get("last_reset_check", datetime.now(UTC).isoformat()))
        return char

# ------------[       MAIN        ]------------ #

def find_character(characters, name, realm):
    n, r = name.lower(), realm.lower()
    return next((c for c in characters if c.name.lower() == n and c.realm.lower() == r), None)


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
                token = get_access_token(region)
                if token:
                    items = fetch_equipment(region, realm, name, token)
                    emit({"status": "equipment", "name": name, "realm": realm, "items": items})
                else:
                    emit({"status": "error", "message": "Could not authenticate"})

        elif command == "EXIT":
            save_data(characters)
            break


if __name__ == "__main__":
    main()