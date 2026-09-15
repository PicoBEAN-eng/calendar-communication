"""Per-instance inert search keys (P2): five lowercase alphanumerics with at least one digit, minted with a
collision check against the stream's calendars, kept in state/keys.json. Import get_or_mint(svc, cfg, name)."""
import json, random, string
from pathlib import Path


def keys_path(cfg) -> Path:
    return Path(cfg["state_file"]).parent / "keys.json"


def load(cfg) -> dict:
    p = keys_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def save(cfg, keys: dict) -> None:
    keys_path(cfg).write_text(json.dumps(keys, indent=1) + "\n")


def free(svc, cals, key: str) -> bool:
    for cal in cals:
        r = svc.events().list(calendarId=cal, q=key, timeMin="1999-01-01T00:00:00Z", timeMax="9999-01-01T00:00:00Z",
                              singleEvents=True, maxResults=3).execute()
        if r.get("items"):
            return False
    return True


def mint(svc, cals) -> str:
    while True:
        k = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
        if k[0].isalpha() and any(c.isdigit() for c in k) and free(svc, cals, k):
            return k


def get_or_mint(svc, cfg, name: str, extra_cals=()) -> str:
    keys = load(cfg)
    if name not in keys:
        keys[name] = mint(svc, [cfg["calendar_id"], *extra_cals])
        save(cfg, keys)
    return keys[name]
