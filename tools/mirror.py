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
import links  # noqa: E402  (tools/links.py: identity keys + link translation)
import structure as st  # noqa: E402  (tools/structure.py: parent tokens, folder-notes, drag translation)

FENCE = re.compile(r"%% vault-only %%.*?%% /vault-only %%\n?", re.S)
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
STABLE_SECONDS = 10   # the relay daemon writes files atomically; the guard only covers our own just-written files
CAP = 7800


def canonical(text: str) -> str:
    return FENCE.sub("", text).strip()


# Link registry (name <-> identity key) over every mirrored note; filled by main() before any sync.
REG = {"name2key": {}, "key2name": {}, "missing": set()}


def register(ev, rel: str):
    key = links.key_of(canonical(ev.get("description") or ""))
    if not key:
        return
    for name in (re.sub(r"^Note:\s*", "", ev.get("summary", "")).strip(), Path(rel).stem):
        REG["name2key"][name] = key
    REG["key2name"][key] = re.sub(r"^Note:\s*", "", ev.get("summary", "")).strip()


def fences(text: str) -> str:
    return "".join(m.group(0) for m in FENCE.finditer(text))


def h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def file_name(title: str) -> str:
    return UNSAFE.sub("-", re.sub(r"^Note:?\s*", "", title)).strip() + ".md"


def stable(path: Path) -> bool:
    return (datetime.now().timestamp() - path.stat().st_mtime) >= STABLE_SECONDS


def write_file(path: Path, canon: str, keep_fences: str, dry: bool):
    body = canon.rstrip() + ("\n\n" + keep_fences.strip() + "\n" if keep_fences.strip() else "\n")
    print(("[dry] " if dry else "") + f"vault  <- calendar  {path.name}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def write_event(svc, cal, ev, canon: str, dry: bool):
    canon = links.up(canon, REG["name2key"], REG["missing"])   # vault form -> calendar form (glued link keys)
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
    e_canon = links.down(canonical(ev.get("description") or ""), REG["key2name"])   # calendar form -> vault form
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


BLOCK_REF = re.compile(r"!?\[\[([^\]\|#]+?)#\^([A-Za-z0-9-]+)(?:\\?\|[^\]]*)?\]\]")


def dangling_blocks(vault: Path, rels) -> list:
    """Block references ([[Name#^id]] / ![[Name#^id]]) among the mirrored files whose ^id is not in the
    target note (searched by basename across the whole vault). Block ids are plain text to the mirror:
    never stripped or reflowed; this only reports where a quote would point at nothing."""
    stems = {}
    for p in vault.rglob("*.md"):
        stems.setdefault(p.stem, []).append(p)
    out = []
    for rel in rels:
        f = vault / rel
        if not f.exists():
            continue
        for name, bid in BLOCK_REF.findall(f.read_text(encoding="utf-8", errors="replace")):
            targets = stems.get(links.basename(name), [])
            if not any(re.search(r"\^" + re.escape(bid) + r"\b", t.read_text(encoding="utf-8", errors="replace")) for t in targets):
                out.append((rel, name, bid))
    return out


def list_future(svc, cal):
    items, page = [], None
    while True:
        r = svc.events().list(calendarId=cal, timeMin="2999-12-01T00:00:00Z", timeMax="9999-01-01T00:00:00Z", singleEvents=True,
                              maxResults=2500, pageToken=page).execute()
        items += r.get("items", []); page = r.get("nextPageToken")
        if not page:
            return [e for e in items if re.match(r"^Note:?\s", e.get("summary", ""))]


def set_cache(svc, cal, n, rel, dry):
    if not dry:
        cp.write_event(svc, cal, n.ev["id"], {"extendedProperties": {"private": {"mirror_path": rel}}}, existing=n.ev, verify=False)
    n.ev.setdefault("extendedProperties", {}).setdefault("private", {})["mirror_path"] = rel
    n.cache = rel


def set_parent(svc, cal, n, parent, dry):
    n.parent = parent
    loc = st.format_location(n.key, parent, n.classes, n.others)
    print(("[dry] " if dry else "") + f"token   <- vault   {n.name}: parent -> {parent}")
    if not dry:
        cp.write_event(svc, cal, n.ev["id"], {"location": loc}, existing=n.ev, verify=False)
    n.ev["location"] = loc


def move_file(vault, n, rel, dry):
    src, dst = vault / n.actual, vault / rel
    print(("[dry] " if dry else "") + f"vault   <- token   {n.name}: {n.actual} -> {rel}")
    if not dry:
        if not src.exists() and dst.exists():   # already carried there by its folder's move
            n.actual = rel; return
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        if n.is_folder:      # a folder-note carries its folder: move the rest of the directory with it
            for child in list(src.parent.iterdir()):
                child.rename(dst.parent / child.name)
            try:
                src.parent.rmdir()
            except OSError:
                pass
    n.actual = rel


def init_structure(svc, cal, vault, nodes, dry):
    """One-time migration: folder-notes for the root and every folder the caches name; p-tokens for
    every note from its cached directory. Existing notes named like their folder become the folder-note."""
    by_title = {n.ev["summary"]: n for n in nodes.values()}
    dirs = sorted({str(Path(n.cache).parent) for n in nodes.values() if n.cache and not n.pinned}, key=lambda d: d.count("/"))
    root_dir = st.ROOT_NAME
    dirs = [d for d in dirs if d == root_dir or d.startswith(root_dir + "/")]
    if root_dir not in dirs:
        dirs.insert(0, root_dir)
    folder_of_dir = {}
    for d in dirs:
        name = Path(d).name
        parent_dir = str(Path(d).parent) if d != root_dir else None
        n = by_title.get(f"Note: {name}")
        if n is None:
            key = links.mint(TAKEN)
            body = f"# {name}\n\n(empty)\n\n---\n{key}\n"
            ev = {"summary": f"Note: {name}", "start": {"date": st.FOLDER_DAY}, "end": {"date": "3000-01-03"},
                  "location": key, "description": body,
                  "extendedProperties": {"private": {"comms_kind": "folder", "comms_writer": "mirror", "mirror_path": f"{d}/{name}.md"}}}
            print(("[dry] " if dry else "") + f"folder-note created: Note: {name} ({d})")
            if not dry:
                ev = cp.insert_event(svc, cal, ev)
            else:
                ev["id"] = f"dry-{key}"; ev["updated"] = "2000-01-01T00:00:00Z"
            n = st.Node(ev); n.key = key; nodes[key] = n; by_title[ev["summary"]] = n
        n.is_folder = True
        n.ev.setdefault("extendedProperties", {}).setdefault("private", {})["comms_kind"] = "folder"
        folder_of_dir[d] = n
        if parent_dir and not n.parent:
            n.parent = folder_of_dir[parent_dir].key
        loc = st.format_location(n.key, n.parent, n.classes, n.others)
        if not dry:
            cp.write_event(svc, cal, n.ev["id"], {"location": loc, "extendedProperties": {"private": {"comms_kind": "folder"}}}, existing=n.ev, verify=False)
        n.ev["location"] = loc      # the cache is left alone: derive() moves the file if the folder-note sits elsewhere
    for n in nodes.values():
        if n.is_folder or n.pinned or not n.cache or n.parent:
            continue
        d = str(Path(n.cache).parent)
        if d in folder_of_dir:
            n.parent = folder_of_dir[d].key
            loc = st.format_location(n.key, n.parent, n.classes, n.others)
            if not dry:
                cp.write_event(svc, cal, n.ev["id"], {"location": loc}, existing=n.ev, verify=False)
            n.ev["location"] = loc
    print(f"init: {len(folder_of_dir)} folder-notes, parents set on {sum(1 for n in nodes.values() if n.parent)} notes")


TAKEN = set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--vault")
    ap.add_argument("--adopt", help="exact title to bring under the mirror")
    ap.add_argument("--layer", help="YYYY-MM-DD of the note's layer day (with --adopt)")
    ap.add_argument("--folder", help="vault folder for that layer (with --adopt)")
    ap.add_argument("--init-structure", action="store_true", help="one-time: folder-notes + parent tokens from the cached paths")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    if not (a.vault or cfg.get("mirror_dir")):
        print("mirror is off for this instance (set mirror_dir in comms.toml); nothing done"); return
    vault = Path(a.vault or cfg["mirror_dir"]).expanduser()
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    events = list_future(svc, cal)
    if a.adopt:
        ev = next((e for e in events if e.get("summary") == a.adopt and (e["start"].get("date") or e["start"]["dateTime"][:10]) == a.layer), None)
        if not ev:
            sys.exit(f"no event titled {a.adopt!r} on {a.layer}")
        ev.setdefault("extendedProperties", {}).setdefault("private", {})["mirror_path"] = f"{a.folder}/{file_name(ev['summary'])}"
        if not a.dry_run:
            cp.write_event(svc, cal, ev["id"], {"extendedProperties": {"private": {"mirror_path": ev["extendedProperties"]["private"]["mirror_path"]}}}, existing=ev, verify=False)

    # ---- structure pass: parent tokens are the truth, paths are derived ------------------------
    nodes = st.build([e for e in events if e.get("extendedProperties", {}).get("private", {}).get("mirror_path") or links.key_of(e.get("description") or "")])
    nodes = {k: n for k, n in nodes.items() if n.cache}      # only mirrored notes have positions
    for n in nodes.values():
        TAKEN.add(n.key)
    if a.init_structure:
        init_structure(svc, cal, vault, nodes, a.dry_run)
    problems = st.derive(nodes)
    actual = st.scan_vault(vault, {k: n.cache for k, n in nodes.items()})
    actions = st.reconcile(nodes, vault, actual, print)
    for kind, n, arg in actions:
        if kind == "move-file":
            move_file(vault, n, arg, a.dry_run); set_cache(svc, cal, n, arg, a.dry_run)
        elif kind == "set-parent":
            set_parent(svc, cal, n, arg, a.dry_run); set_cache(svc, cal, n, n.actual, a.dry_run)
        elif kind == "cache":
            set_cache(svc, cal, n, arg, a.dry_run)
    if actions:
        problems = st.derive(nodes)          # parents may have changed: re-derive before listings
        for n in nodes.values():
            if not n.pinned and n.derived and n.cache != n.derived:
                set_cache(svc, cal, n, n.derived, a.dry_run)
    for n, why in problems:
        print(f"structure: {n.name}: {why}")

    targets = [(n.ev, n.cache) for n in nodes.values()]
    for ev, rel in targets:
        register(ev, rel)

    # ---- folder listings: generated on the nexus, written to both sides ------------------------
    for n in nodes.values():
        if not n.is_folder or n.pinned:
            continue
        gen = st.listing(n, nodes)
        path = vault / n.cache
        current = path.read_text(encoding="utf-8") if path.exists() else (n.ev.get("description") or "")
        keep = fences(current)
        new = st.with_listing(canonical(current), gen, n.key)
        if canonical(new) != canonical(current) or not path.exists():
            print(("[dry] " if a.dry_run else "") + f"listing -> both    {n.name}")
            write_file(path, new, keep, a.dry_run)
            if not a.dry_run:
                cp.write_event(svc, cal, n.ev["id"], {"description": links.up(new, REG["name2key"], REG["missing"]),
                               "extendedProperties": {"private": {"mirror_hash": h(new)}}}, existing=n.ev, verify=False)
            n.ev["description"] = new
            n.ev.setdefault("extendedProperties", {}).setdefault("private", {})["mirror_hash"] = h(new)

    # ---- content pass --------------------------------------------------------------------------
    changed = 0
    for ev, rel in targets:
        res = mirror_one(svc, cal, vault, ev, rel, a.dry_run)
        if res != "in sync":
            print(f"{ev['summary']}: {res}")
        if res not in ("in sync",) and not res.startswith("skipped"):
            changed += 1
    dangling = dangling_blocks(vault, [rel for _, rel in targets])
    for rel, name, bid in dangling:
        print(f"dangling block reference: {rel} -> [[{name}#^{bid}]] (no such block id in the target)")
    print(f"mirror pass: {len(targets)} notes, {changed} changed, {len(actions)} structural actions, {len(problems)} structure notes"
          + (f"; {len(REG['missing'])} link targets without a key stayed name-only" if REG["missing"] else "")
          + (f"; {len(dangling)} dangling block references" if dangling else ""))
    sys.exit(3 if (changed or actions) else 0)   # exit 3 = something moved; the timer's settle loop runs again


if __name__ == "__main__":
    main()
