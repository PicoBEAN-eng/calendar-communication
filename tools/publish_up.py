#!/usr/bin/env python3
"""One-way publisher: Obsidian vault -> calendar, with FOLDER IDENTITY (agreed spec 2026-09-19,
"Note: Folder identity for the publisher · agreed spec 2026-09-19", operator's go the same day).

Obsidian is the source of truth; the calendar is a read mirror. Nothing is ever written into the vault:
identity lives in a SIDECAR on this machine (state/publish.json), which is a CACHE rebuildable from the
calendar (--rebuild-sidecar) because every event carries its keys and hash in private properties.

Calendar side = Frames' model exactly: a note event's location reads "<note key> p<folder key>"; a
folder's index note carries "<folder key>" (+ "p<parent key>" when nested under another mapped folder)
and comms_kind "folder". The two-way mirror can take over later with no migration.

Config (comms.toml):
    publish_dir     = "/path/to/vault"
    publish_layers  = ["The Drawing Board=3030", "WoollyWorkplace/The Office=3050|Office",
                       "- ?? ?? ????-??-??=3090|Heartbeat", "k:ab12c=3030"]
    publish_exclude = ["WoollyWorkplace/junk"]
A spec is a vault-relative folder path (nested allowed), a glob PATTERN (identity lives in the mapping:
the newest matching directory by the timestamp in its name is the folder, extras are reported and
never deleted; a label is required), or k:<folder key> once the dry run has printed the key.
"|Label" fixes the index note's title.

Rename/move detection, each pass, in order: recorded path exists -> same folder; old path absent and a
directory carries the recorded inode -> same folder (a local rename; ext4 keeps the inode); a directory
holding a MAJORITY of the recorded notes by key (hash first, then inode), old path absent -> same folder
(a rename from another device arrives as delete+create); else MISSING: reported, events kept, pending
deletes listed every pass, deleted only after MISSING_PASSES consecutive misses with the events untouched
on the calendar in that window, or on explicit unmapping (also held with the same grace).
Notes: path, else hash, else inode, else (pattern folders) basename -> same note; new -> mint; gone ->
delete. A rename retitles only what carries the name; a move between mapped layers rewrites the p-token.
Never delete-and-reinsert.

Long notes: links are expanded first (glued keys), then measured, then split at paragraph boundaries;
a table split repeats its header rows per part. Pictures never travel (inline <svg> dropped).

  publish_up.py [--apply] [--rebuild-sidecar] [--config comms.toml]      (dry run by default)
"""
import argparse, fnmatch, hashlib, json, os, re, sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402
import links  # noqa: E402

FENCE = re.compile(r"%% vault-only %%.*?%% /vault-only %%\n?", re.S)
SVG = re.compile(r"<svg\b.*?</svg>", re.S | re.I)
TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}")
WRITER = "publish_up"
BODY_CAP = 7600
INDEX_DAY = "3000-01-02"   # default; main() derives the real value from note_anchor_date + 1 day (2026-09-22)
MISSING_PASSES = 3
MAX_DEPTH = 4


def h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def canonical(text: str) -> str:
    return SVG.sub("[picture omitted: it is regenerated in the vault]", FENCE.sub("", text)).strip()


def inode(p: Path):
    try:
        st = p.stat(); return [st.st_dev, st.st_ino]
    except OSError:
        return None


def name_stamp(name: str):
    """Sort key for pattern folders: the date and time found in the name, then the name."""
    d = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    t = re.search(r"(?<!\d)(\d{2})[ :._-](\d{2})(?!\d)", name.replace(d.group(0), "") if d else name)
    return (d.group(0) if d else "", f"{t.group(1)}:{t.group(2)}" if t else "", name)


# ---- splitting -------------------------------------------------------------------------------
def split_parts(text: str, cap: int) -> list:
    """Paragraph-boundary split; an oversized table paragraph is cut at row boundaries with its header
    rows repeated per part; any other oversized paragraph is cut at the last newline or space."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        piece = (cur + "\n\n" + para) if cur else para
        if len(piece) <= cap:
            cur = piece; continue
        if cur:
            parts.append(cur); cur = ""
        lines = para.split("\n")
        if len(para) > cap and len(lines) > 2 and lines[0].startswith("|") and TABLE_SEP.match(lines[1]):
            header = lines[0] + "\n" + lines[1]
            chunk = header
            for row in lines[2:]:
                if len(chunk) + 1 + len(row) > cap:
                    parts.append(chunk); chunk = header
                chunk += "\n" + row
            cur = chunk
            continue
        while len(para) > cap:
            cut = max(para.rfind("\n", 0, cap), para.rfind(" ", 0, cap))
            cut = cut if cut > cap // 2 else cap
            parts.append(para[:cut].rstrip()); para = para[cut:].lstrip()
        cur = para
    if cur:
        parts.append(cur)
    return parts or [""]


# ---- sidecar ---------------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    if not path.exists():
        return {"v": 2, "folders": {}, "notes": {}}
    st = json.loads(path.read_text())
    if "v" not in st:      # v1: flat path -> note entry
        st = {"v": 2, "folders": {}, "notes": st}
    return st


def list_events(svc, cal):
    items, page = [], None
    while True:
        r = svc.events().list(calendarId=cal, timeMin="2999-12-01T00:00:00Z", timeMax="9999-01-01T00:00:00Z", singleEvents=True,
                              maxResults=2500, pageToken=page).execute()
        items += r.get("items", []); page = r.get("nextPageToken")
        if not page:
            return items


def rebuild_sidecar(mine: dict) -> dict:
    """The sidecar from the calendar alone: every event this publisher wrote carries its keys."""
    st = {"v": 2, "folders": {}, "notes": {}}
    for e in mine.values():
        priv = e.get("extendedProperties", {}).get("private", {})
        if priv.get("publish_index"):
            fk = priv.get("publish_folder")
            toks = (e.get("location") or "").split()
            parent = next((t[1:] for t in toks if len(t) == 6 and t[0] == "p"), None)
            st["folders"][fk] = {"path": priv.get("publish_path", "").rstrip("/"), "layer": priv.get("publish_layer", ""),
                                 "label": priv.get("publish_label", ""), "parent": parent, "index_id": e["id"],
                                 "inode": None, "spec": priv.get("publish_spec", ""), "missing": 0}
        else:
            rel = priv.get("publish_path")
            if not rel:
                continue
            n = st["notes"].setdefault(rel, {"key": priv.get("publish_key"), "event_ids": [], "hash": priv.get("publish_hash"),
                                             "folder": priv.get("publish_folder"), "inode": None, "title": None})
            part = int(priv.get("publish_part") or 1)
            while len(n["event_ids"]) < part:
                n["event_ids"].append(None)
            n["event_ids"][part - 1] = e["id"]
    return st


# ---- verify: the silent-failure tripwire (read-only) -------------------------------------------
def verify(vault: Path, state: dict, mine: dict, stale_minutes: int) -> int:
    """Compares what the vault holds now with what the calendar holds, never with 'changed since last
    pass' (the heartbeat changes every pass by design). A stall = a file whose current hash differs from
    its calendar copy's publish_hash AND whose mtime is older than stale_minutes (the timer had its
    chance); or a calendar copy that is missing. Also prints the age of each folder's newest update."""
    now = datetime.now(timezone.utc); stalls = 0; checked = 0; newest = {}
    for rel, n in state.get("notes", {}).items():
        p = vault / rel
        ids = [i for i in (n.get("event_ids") or []) if i]
        if not p.exists():
            continue        # a gone file is the next pass's delete, not a stall
        checked += 1
        try:
            cur = h(canonical(p.read_text(encoding="utf-8", errors="replace")))
            age_min = (now - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
        except OSError:
            continue
        cal_hashes = {mine[i].get("extendedProperties", {}).get("private", {}).get("publish_hash") for i in ids if i in mine}
        missing = [i for i in ids if i not in mine]
        for i in ids:
            if i in mine:
                u = mine[i].get("updated", ""); fk = n.get("folder")
                newest[fk] = max(newest.get(fk, ""), u)
        if missing:
            print(f"STALL  {rel}: {len(missing)} calendar copy missing"); stalls += 1
        elif cur not in cal_hashes and age_min > stale_minutes:
            print(f"STALL  {rel}: vault changed {age_min:.0f} min ago, calendar copy still older"); stalls += 1
    for fk, f in state.get("folders", {}).items():
        u = newest.get(fk)
        if u:
            age = (now - datetime.fromisoformat(u.replace("Z", "+00:00"))).total_seconds() / 60
            print(f"folder {f.get('label') or f.get('spec')}: newest calendar update {age:.0f} min ago")
    print(f"verify: {checked} files checked, {stalls} stalls")
    return 2 if stalls else 0


# ---- resolution -------------------------------------------------------------------------------
def parse_layers(cfg) -> list:
    out = []
    for item in cfg["publish_layers"]:
        spec, rest = item.split("=", 1)
        year, _, label = rest.partition("|")
        out.append((spec.strip(), year.strip(), label.strip()))
    return out


def all_dirs(vault: Path) -> list:
    out = []
    for p in vault.rglob("*"):
        if p.is_dir() and not p.name.startswith(".") and len(p.relative_to(vault).parts) <= MAX_DEPTH:
            out.append(p)
    return out


def notes_under(vault: Path, folder: Path, excludes: list, other_mapped: list) -> dict:
    out = {}
    for p in sorted(folder.rglob("*.md")):
        rel = str(p.relative_to(vault))
        if any(rel == x or rel.startswith(x + "/") for x in excludes):
            continue
        if any(rel.startswith(o + "/") for o in other_mapped):
            continue
        out[rel] = p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rebuild-sidecar", action="store_true", help="rebuild state/publish.json from the calendar, then continue")
    ap.add_argument("--verify", action="store_true", help="read-only tripwire: file hash vs calendar copy, stamp ages; exit 2 on a stall")
    ap.add_argument("--stale-minutes", type=int, default=25, help="with --verify: a change unpublished for longer than this is a stall")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    dry = not a.apply
    tag = "[dry] " if dry else ""
    cfg = cp.load_config(Path(a.config))
    if not cfg.get("publish_dir") or not cfg.get("publish_layers"):
        print("publisher is off (set publish_dir and publish_layers in comms.toml); nothing done"); return
    vault = Path(cfg["publish_dir"]).expanduser()
    # The index day is the day after the stream's anchor date (config), not a constant: the band can move by config alone.
    _anchor = date.fromisoformat(cfg.get("note_anchor_date") or "3000-01-01")
    index_day = (_anchor + timedelta(days=1)).isoformat(); index_end = (_anchor + timedelta(days=2)).isoformat()
    layers = parse_layers(cfg)
    excludes = [x.strip("/") for x in (cfg.get("publish_exclude") or [])]
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    state_path = Path(cfg.get("publish_state") or (CC / "state" / "publish.json"))

    events = list_events(svc, cal)
    mine = {e["id"]: e for e in events if e.get("extendedProperties", {}).get("private", {}).get("comms_writer") == WRITER}
    state = rebuild_sidecar(mine) if a.rebuild_sidecar else load_state(state_path)
    if a.verify:
        sys.exit(verify(vault, state, mine, a.stale_minutes))
    if a.rebuild_sidecar:
        print(f"sidecar rebuilt from the calendar: {len(state['folders'])} folders, {len(state['notes'])} notes")
    folders, notes = state["folders"], state["notes"]
    taken = {t for e in events for t in (e.get("location") or "").split() if links.is_key(t)}
    taken |= {v["key"] for v in notes.values() if v.get("key")} | set(folders)
    counts = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0, "split": 0, "overwrote calendar edit": 0,
              "folders renamed": 0, "notes moved": 0, "pending deletes": 0}
    now = datetime.now(timezone.utc).isoformat()
    dirs = all_dirs(vault)
    dir_by_inode = {tuple(inode(d)): d for d in dirs if inode(d)}
    problems = []

    # ---- 1. resolve every mapping to a folder key + directory --------------------------------
    resolved = {}          # fkey -> (dir Path | None, year, label, spec)
    claimed = set()
    for spec, year, label in layers:
        fk = None; d = None
        if spec.startswith("k:"):
            fk = spec[2:]
            if fk not in folders:
                problems.append(f"unknown folder key in config: {spec}"); continue
            spec = folders[fk].get("spec") or spec
        else:
            fk = next((k for k, f in folders.items() if f.get("spec") == spec), None)
        is_pattern = any(ch in spec for ch in "*?[")
        if is_pattern:
            if not label:
                problems.append(f"pattern mapping {spec} needs a |Label"); continue
            matches = [x for x in dirs if fnmatch.fnmatch(str(x.relative_to(vault)), spec) and x not in claimed]
            if matches:
                matches.sort(key=lambda x: name_stamp(x.name))
                d = matches[-1]
                for extra in matches[:-1]:
                    problems.append(f"{label}: extra directory matching the pattern left alone: {extra.relative_to(vault)}")
        else:
            cand = vault / spec
            if cand.is_dir():
                d = cand
            elif fk and folders[fk].get("path") and (vault / folders[fk]["path"]).is_dir():
                d = vault / folders[fk]["path"]
        if d is None and fk:
            f = folders[fk]
            ino = tuple(f["inode"]) if f.get("inode") else None
            if ino and ino in dir_by_inode and dir_by_inode[ino] not in claimed:
                d = dir_by_inode[ino]; counts["folders renamed"] += 1
                print(f"{tag}folder  renamed (same inode): {f['path']} -> {d.relative_to(vault)}")
            else:
                mine_notes = {rel: n for rel, n in notes.items() if n.get("folder") == fk}
                if mine_notes:
                    want_h = {n["hash"]: rel for rel, n in mine_notes.items() if n.get("hash")}
                    want_i = {tuple(n["inode"]): rel for rel, n in mine_notes.items() if n.get("inode")}
                    best, best_n = None, 0
                    for x in dirs:
                        if x in claimed:
                            continue
                        hit = 0
                        for p in x.glob("*.md"):
                            try:
                                hh = h(canonical(p.read_text(encoding="utf-8", errors="replace")))
                            except OSError:
                                continue
                            if hh in want_h or (inode(p) and tuple(inode(p)) in want_i):
                                hit += 1
                        if hit > best_n:
                            best, best_n = x, hit
                    if best is not None and best_n * 2 > len(mine_notes):
                        d = best; counts["folders renamed"] += 1
                        print(f"{tag}folder  moved (majority of its notes, {best_n}/{len(mine_notes)}): {f['path']} -> {d.relative_to(vault)}")
        if fk is None:
            fk = links.mint(taken)
            folders[fk] = {"path": None, "layer": year, "label": label, "parent": None, "index_id": None, "inode": None,
                           "spec": spec, "missing": 0}
            print(f"{tag}folder  new mapping {spec} -> key {fk}")
        f = folders[fk]; f["layer"] = year; f["label"] = label; f["spec"] = spec
        if d is not None:
            claimed.add(d)
            newrel = str(d.relative_to(vault))
            if f.get("path") and f["path"] != newrel:
                if any(ch in spec for ch in "*?["):
                    counts["folders renamed"] += 1
                    print(f"{tag}folder  renamed (pattern re-resolved, identity from the mapping): {f['path']} -> {newrel}")
            f["path"] = newrel; f["inode"] = inode(d); f["missing"] = 0; f.pop("missing_since", None)
        else:
            f["missing"] = f.get("missing", 0) + 1; f.setdefault("missing_since", now)
            problems.append(f"folder MISSING ({f['missing']}/{MISSING_PASSES}): {spec} (key {fk}, last at {f.get('path')})")
        resolved[fk] = (d, year, label, spec)
    # unmapped folders (in the sidecar, no longer in config) are held with the same grace
    for fk, f in folders.items():
        if fk not in resolved:
            f["missing"] = f.get("missing", 0) + 1; f.setdefault("missing_since", now)
            problems.append(f"folder UNMAPPED ({f['missing']}/{MISSING_PASSES}): {f.get('spec')} (key {fk})")
    # parents: the longest other mapped path that prefixes this one
    paths = {fk: f["path"] for fk, f in folders.items() if f.get("path")}
    for fk, f in folders.items():
        p = f.get("path") or ""
        parent = max((ok for ok, op in paths.items() if ok != fk and p.startswith(op + "/")), key=lambda ok: len(paths[ok]), default=None)
        f["parent"] = parent

    # ---- 2. notes: identity by path, hash, inode, basename -------------------------------------
    files = {}     # rel -> dict(folder, text, hash, inode, stem, parent)
    for fk, (d, year, label, spec) in resolved.items():
        if d is None:
            continue
        others = [op for ok, op in paths.items() if ok != fk]
        for rel, p in notes_under(vault, d, excludes, others).items():
            raw = p.read_text(encoding="utf-8", errors="replace")
            text = canonical(raw)
            files[rel] = {"folder": fk, "text": text, "hash": h(text), "inode": inode(p), "stem": p.stem, "parent": p.parent.name,
                          "pattern": any(ch in spec for ch in "*?[")}
    unmatched = {rel: n for rel, n in notes.items() if rel not in files}
    by_hash = {n["hash"]: rel for rel, n in unmatched.items() if n.get("hash")}
    by_inode = {tuple(n["inode"]): rel for rel, n in unmatched.items() if n.get("inode")}
    by_base = {}
    for rel, n in unmatched.items():
        by_base.setdefault((n.get("folder"), Path(rel).name), rel)
    for rel, f in files.items():
        if rel in notes:
            continue
        old = by_hash.get(f["hash"]) or (by_inode.get(tuple(f["inode"])) if f["inode"] else None)
        if old is None and f["pattern"]:
            old = by_base.get((f["folder"], Path(rel).name))
        if old and old in notes and old not in files:
            notes[rel] = notes.pop(old)
            old_dir, new_dir = str(Path(old).parent), str(Path(rel).parent)
            what = ("moved" if notes[rel].get("folder") != f["folder"]
                    else "path updated (its folder was renamed)" if old_dir != new_dir and Path(old).name == Path(rel).name
                    else "renamed")
            counts["notes moved"] += 1
            print(f"{tag}note    {what}: {old} -> {rel}")
        else:
            notes[rel] = {"key": links.mint(taken), "event_ids": [], "hash": None, "folder": f["folder"], "inode": None, "title": None}
    # titles: stem, disambiguated by parent folder when a stem repeats in the published set
    seen = {}
    for rel, f in files.items():
        seen.setdefault(f["stem"], []).append(rel)
    for rel, f in files.items():
        f["title"] = f"Note: {f['stem']}" + (f" ({f['parent']})" if len(seen[f["stem"]]) > 1 else "")
    name2key = {f["stem"]: notes[rel]["key"] for rel, f in files.items()}
    for e in events:
        k = links.key_of(e.get("description") or "")
        if k:
            name2key.setdefault(re.sub(r"^Note:?\s*", "", e["summary"]).strip(), k)

    # ---- 3. publish notes ------------------------------------------------------------------------
    missing_links = set()
    rows = {}
    for rel, f in files.items():
        n = notes[rel]; key = n["key"]; fk = f["folder"]
        n["folder"] = fk; n["inode"] = f["inode"]
        expanded = links.up(f["text"], name2key, missing_links)
        parts = split_parts(expanded, BODY_CAP)
        if len(parts) > 1:
            counts["split"] += 1; print(f"{tag}split   {rel}: {len(expanded)} expanded chars -> {len(parts)} parts")
        day = f"{folders[fk]['layer']}-01-01"
        m = len(parts)
        titles = [f["title"] if m == 1 else f"{f['title']} · part {i + 1} of {m}" for i in range(m)]
        rows.setdefault(fk, []).append((f["title"], day, m))
        bodies = [p.rstrip() + "\n\n" + key + "\n" for p in parts]
        ids = [i for i in (n.get("event_ids") or []) if i]
        for eid in ids[m:]:
            print(f"{tag}delete  extra part of {rel}")
            if not dry and eid in mine:
                cp.pace(); svc.events().delete(calendarId=cal, eventId=eid).execute()
            counts["deleted"] += 1
        ids = ids[:m]
        loc = f"{key} p{fk}"
        for i, (title, body) in enumerate(zip(titles, bodies)):
            priv = {"comms_kind": "note", "comms_writer": WRITER, "publish_path": rel, "publish_hash": f["hash"], "publish_part": str(i + 1),
                    "publish_parts": str(m), "publish_key": key, "publish_folder": fk}
            ev_body = {"summary": title, "start": {"date": day}, "end": {"date": date.fromisoformat(day).replace(day=2).isoformat()},
                       "location": loc, "description": body, "extendedProperties": {"private": priv}}
            cp.check_cap(body, title)
            if i < len(ids) and ids[i] in mine:
                cur = mine[ids[i]]
                cur_priv = cur.get("extendedProperties", {}).get("private", {})
                same = (cur.get("summary") == title and (cur.get("description") or "") == body
                        and cur["start"].get("date") == day and (cur.get("location") or "") == loc)
                if same and all(cur_priv.get(k) == v for k, v in priv.items()):
                    counts["unchanged"] += 1; continue
                if same:      # only the bookkeeping moved (a rename or move): patch the private properties, nothing else
                    if not dry:
                        cp.write_event(svc, cal, ids[i], {"extendedProperties": {"private": priv}}, existing=cur, verify=False)
                    counts["unchanged"] += 1; continue
                if n.get("hash") == f["hash"] and (cur.get("description") or "") != body and cur.get("summary") == title:
                    counts["overwrote calendar edit"] += 1
                print(f"{tag}update  {title}")
                if not dry:
                    cp.write_event(svc, cal, ids[i], ev_body, replace_meta=True)
                counts["updated"] += 1
            else:
                print(f"{tag}insert  {title} ({day})")
                if not dry:
                    ev = cp.insert_event(svc, cal, ev_body)
                    if i < len(ids):
                        ids[i] = ev["id"]
                    else:
                        ids.append(ev["id"])
                counts["inserted"] += 1
        n["event_ids"] = ids; n["hash"] = f["hash"]; n["title"] = f["title"]

    # ---- 4. notes gone from present folders: delete -------------------------------------------
    present = {fk for fk, (d, *_rest) in resolved.items() if d is not None}
    for rel in [r for r in notes if r not in files]:
        n = notes[rel]
        if n.get("folder") in present:
            for eid in n.get("event_ids") or []:
                print(f"{tag}delete  {n.get('title') or rel} (file gone)")
                if not dry and eid in mine:
                    cp.pace(); svc.events().delete(calendarId=cal, eventId=eid).execute()
                counts["deleted"] += 1
            del notes[rel]

    # ---- 5. missing / unmapped folders: hold, list, delete after the grace ---------------------
    for fk in list(folders):
        f = folders[fk]
        if f.get("missing", 0) == 0:
            continue
        held = [rel for rel, n in notes.items() if n.get("folder") == fk]
        ids = [eid for rel in held for eid in (notes[rel].get("event_ids") or [])] + ([f["index_id"]] if f.get("index_id") else [])
        counts["pending deletes"] += len(ids)
        since = f.get("missing_since") or now
        touched = [eid for eid in ids if eid in mine and mine[eid].get("updated", "") > since]
        for eid in ids:
            print(f"{tag}pending delete ({f['missing']}/{MISSING_PASSES}): {mine[eid]['summary'] if eid in mine else eid}")
        if f["missing"] >= MISSING_PASSES and not touched:
            for eid in ids:
                print(f"{tag}delete  {mine[eid]['summary'] if eid in mine else eid} (folder gone {MISSING_PASSES} passes, untouched)")
                if not dry and eid in mine:
                    cp.pace(); svc.events().delete(calendarId=cal, eventId=eid).execute()
                counts["deleted"] += 1
            for rel in held:
                del notes[rel]
            del folders[fk]
        elif touched:
            problems.append(f"folder {f.get('spec')}: events touched on the calendar since it went missing; delete withheld")

    # ---- 6. index notes: one per present folder, key in location, parent token when nested ----
    for fk, (d, year, label, spec) in resolved.items():
        if d is None:
            continue
        f = folders[fk]
        name = label or d.name
        title = f"Note: {name} index"
        rws = sorted(rows.get(fk, []))
        lines = [f"Index of the {name} folder, published one-way from Obsidian ({len(rws)} notes). "
                 "Open one by searching its exact title with the day pinned to the date shown; the calendar copy is read-only, "
                 "edits belong in Obsidian.", ""]
        lines += [f"- {t} ({dd})" + (f" · {m} parts" if m > 1 else "") for t, dd, m in rws]
        lines += ["", f"Updated {date.today().isoformat()}", "", fk]
        body = "\n".join(lines)
        loc = fk + (f" p{f['parent']}" if f.get("parent") else "")
        priv = {"comms_kind": "folder", "comms_writer": WRITER, "publish_index": "1", "publish_folder": fk, "publish_path": f["path"] + "/",
                "publish_layer": year, "publish_label": label, "publish_spec": spec}
        ev_body = {"summary": title, "start": {"date": index_day}, "end": {"date": index_end}, "location": loc, "description": body,
                   "extendedProperties": {"private": priv}}
        cp.check_cap(body, title)
        cur = mine.get(f.get("index_id")) if f.get("index_id") else next((e for e in mine.values() if e.get("summary") == title), None)
        if cur and (cur.get("description") or "") == body and cur.get("summary") == title and (cur.get("location") or "") == loc:
            f["index_id"] = cur["id"]; continue
        print(f"{tag}{'update' if cur else 'insert'}  {title}")
        if not dry:
            if cur:
                cp.write_event(svc, cal, cur["id"], ev_body, replace_meta=True); f["index_id"] = cur["id"]
            else:
                f["index_id"] = cp.insert_event(svc, cal, ev_body)["id"]

    for pr in problems:
        print(f"{tag}note: {pr}")
    if not dry:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print(f"{tag}publish pass: {len(files)} files in {len(present)} folders, {counts}"
          + (f"; {len(missing_links)} link targets without a key stayed name-only" if missing_links else ""))
    for fk, f in folders.items():
        print(f"{tag}folder key {fk}: {f.get('spec')} -> {f.get('path')} (layer {f.get('layer')}{', label ' + f['label'] if f.get('label') else ''}{', parent ' + f['parent'] if f.get('parent') else ''})")


if __name__ == "__main__":
    main()
