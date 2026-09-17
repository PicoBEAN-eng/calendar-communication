#!/usr/bin/env python3
"""Vault <-> calendar mirror (prototype). Both surfaces are peers; no database in the middle.

Scope: events that carry the private property mirror_path (set on first mirroring), plus any event
selected with --adopt "<exact title>" on the layer given by --layer (YYYY-MM-DD) into --folder.
Identity: title + folder. File name = title without the leading "Note: ", unsafe characters translated.
Sync memory: private properties mirror_path and mirror_hash (sha1 of the canonical body at last sync).
Canonical body = text with %% vault-only %% ... %% /vault-only %% regions removed; those regions live
only in the vault and are re-attached on every write-back, so calendar -> vault merges, never overwrites.

Change detection: canonical bodies equal -> nothing (echo-proof). Else consult the stored hash: one side
still at the hash -> the other side changed, copy it over. Both moved -> conflict: last writer wins
(file mtime vs event updated), the loser is written to <vault>/_conflicts/, never discarded.
Files whose mtime changed in the last STABLE_SECONDS are skipped (Syncthing may still be writing).

  mirror.py [--vault DIR] [--adopt TITLE --layer YYYY-MM-DD --folder NAME] [--dry-run]
"""
import argparse, hashlib, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC))
import comms_poller as cp  # noqa: E402

FENCE = re.compile(r"%% vault-only %%.*?%% /vault-only %%\n?", re.S)
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
STABLE_SECONDS = 10   # the relay daemon writes files atomically; the guard only covers our own just-written files
CAP = 7800


def canonical(text: str) -> str:
    return FENCE.sub("", text).strip()


def fences(text: str) -> str:
    return "".join(m.group(0) for m in FENCE.finditer(text))


def h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def file_name(title: str) -> str:
    return UNSAFE.sub("-", re.sub(r"^Note:\s*", "", title)).strip() + ".md"


def stable(path: Path) -> bool:
    return (datetime.now().timestamp() - path.stat().st_mtime) >= STABLE_SECONDS


def write_file(path: Path, canon: str, keep_fences: str, dry: bool):
    body = canon.rstrip() + ("\n\n" + keep_fences.strip() + "\n" if keep_fences.strip() else "\n")
    print(("[dry] " if dry else "") + f"vault  <- calendar  {path.name}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def write_event(svc, cal, ev, canon: str, dry: bool):
    if len(canon) > CAP:
        print(f"SKIP {ev['summary']}: {len(canon)} chars exceeds the cap; part n of m splitting is the next increment (flagged, never truncated)")
        return False
    print(("[dry] " if dry else "") + f"calendar <- vault  {ev['summary']}")
    if not dry:
        cp.write_event(svc, cal, ev["id"], {"description": canon}, existing=ev)
    return True


def set_memory(svc, cal, ev, rel: str, hsh: str, dry: bool):
    if not dry:
        cp.write_event(svc, cal, ev["id"], {"extendedProperties": {"private": {"mirror_path": rel, "mirror_hash": hsh}}}, existing=ev, verify=False)


def conflict_save(vault: Path, rel: str, loser_text: str, who: str, dry: bool):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = vault / "_conflicts" / f"{Path(rel).stem} · {stamp} · {who}.md"
    print(("[dry] " if dry else "") + f"conflict: {who} version kept at _conflicts/{p.name}")
    if not dry:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(loser_text, encoding="utf-8")


def mirror_one(svc, cal, vault: Path, ev, rel: str, dry: bool):
    path = vault / rel
    priv = ev.get("extendedProperties", {}).get("private", {})
    last = priv.get("mirror_hash")
    e_canon = canonical(ev.get("description") or "")
    if not path.exists():
        write_file(path, e_canon, "", dry)
        set_memory(svc, cal, ev, rel, h(e_canon), dry)
        return "created in vault"
    if not stable(path):
        return "skipped: file changed in the last 30 s (Syncthing may be mid-write)"
    raw = path.read_text(encoding="utf-8")
    f_canon, keep = canonical(raw), fences(raw)
    if e_canon == f_canon:
        if last != h(e_canon):
            set_memory(svc, cal, ev, rel, h(e_canon), dry)
        return "in sync"
    he, hf = h(e_canon), h(f_canon)
    if hf == last and he != last:
        write_file(path, e_canon, keep, dry); set_memory(svc, cal, ev, rel, he, dry); return "calendar -> vault"
    if he == last and hf != last:
        if write_event(svc, cal, ev, f_canon, dry):
            set_memory(svc, cal, ev, rel, hf, dry)
        return "vault -> calendar"
    # both moved: last writer wins, loser kept
    ev_t = datetime.fromisoformat(ev["updated"].replace("Z", "+00:00"))
    f_t = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if f_t >= ev_t:
        conflict_save(vault, rel, e_canon, "calendar", dry)
        if write_event(svc, cal, ev, f_canon, dry):
            set_memory(svc, cal, ev, rel, hf, dry)
        return "conflict: vault won"
    conflict_save(vault, rel, raw, "vault", dry)
    write_file(path, e_canon, keep, dry); set_memory(svc, cal, ev, rel, he, dry)
    return "conflict: calendar won"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--vault")
    ap.add_argument("--adopt", help="exact title to bring under the mirror")
    ap.add_argument("--layer", help="YYYY-MM-DD of the note's layer day (with --adopt)")
    ap.add_argument("--folder", help="vault folder for that layer (with --adopt)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    if not (a.vault or cfg.get("mirror_dir")):
        print("mirror is off for this instance (set mirror_dir in comms.toml); nothing done"); return
    vault = Path(a.vault or cfg["mirror_dir"]).expanduser()
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    targets = []
    if a.adopt:
        r = svc.events().list(calendarId=cal, timeMin=f"{a.layer}T00:00:00Z", timeMax=f"{a.layer}T23:59:59Z", singleEvents=True, maxResults=250).execute()
        ev = next((e for e in r.get("items", []) if e.get("summary") == a.adopt), None)
        if not ev:
            sys.exit(f"no event titled {a.adopt!r} on {a.layer}")
        targets.append((ev, f"{a.folder}/{file_name(ev['summary'])}"))
    # everything already under the mirror: events carrying mirror_path (search the structural region)
    r = svc.events().list(calendarId=cal, timeMin="3000-01-01T00:00:00Z", timeMax="3050-01-01T00:00:00Z", singleEvents=True,
                          privateExtendedProperty=None, maxResults=2500).execute()
    for ev in r.get("items", []):
        rel = ev.get("extendedProperties", {}).get("private", {}).get("mirror_path")
        if rel and all(ev["id"] != t[0]["id"] for t in targets):
            targets.append((ev, rel))
    changed = 0
    for ev, rel in targets:
        res = mirror_one(svc, cal, vault, ev, rel, a.dry_run)
        print(f"{ev['summary']}: {res}")
        if res not in ("in sync",) and not res.startswith("skipped"):
            changed += 1
    print(f"mirror pass: {len(targets)} notes, {changed} changed")
    sys.exit(3 if changed else 0)   # exit 3 = something moved; the timer's settle loop runs again


if __name__ == "__main__":
    main()
