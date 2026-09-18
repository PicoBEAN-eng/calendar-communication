#!/usr/bin/env python3
"""One-way publisher: Obsidian vault -> calendar (Woolly phase one, approved 2026-09-18).

Obsidian is the source of truth; the calendar is a read mirror. Nothing is ever written into the vault:
identity lives in a SIDECAR on this machine (state/publish.json: path -> key, event ids, content hash).
A calendar-side edit to a published note is overwritten on the next pass. A deleted file deletes its
events. A rename (same content, new path) retitles instead of recreating.

Config (comms.toml):
    publish_dir    = "/path/to/vault"
    publish_layers = ["Design=3030", "Notes=3050"]     # top-level vault folder = layer year (YYYY-01-01)
Titles: "Note: <file stem>" (a stem seen twice gets " (<parent folder>)"). Each folder gets one dated
index note on the index day (the only place dates appear).
Long notes: links are expanded FIRST (glued keys, see tools/links.py), then the expanded text is
measured; over the cap it is split at paragraph boundaries into "· part n of m" events. Files the
operator carved by hand are just files; they are never re-flowed (an oversized carved part still
splits, and is reported). Pictures never travel: inline <svg> bodies are dropped.

  publish_up.py [--apply] [--config comms.toml]      (dry run by default)
"""
import argparse, hashlib, json, re, sys
from datetime import date
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402
import links  # noqa: E402

FENCE = re.compile(r"%% vault-only %%.*?%% /vault-only %%\n?", re.S)
SVG = re.compile(r"<svg\b.*?</svg>", re.S | re.I)
UNSAFE_TITLE = re.compile(r"\s+")
WRITER = "publish_up"
BODY_CAP = 7600          # expanded chars per event body, leaving room for the key line and a margin under 8192
INDEX_DAY = "3000-01-02"


def h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def canonical(text: str) -> str:
    return SVG.sub("[picture omitted: it is regenerated in the vault]", FENCE.sub("", text)).strip()


def split_parts(text: str, cap: int) -> list:
    """Paragraph-boundary split; a single paragraph over the cap is cut at the last newline or space."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        piece = (cur + "\n\n" + para) if cur else para
        if len(piece) <= cap:
            cur = piece; continue
        if cur:
            parts.append(cur); cur = ""
        while len(para) > cap:
            cut = max(para.rfind("\n", 0, cap), para.rfind(" ", 0, cap))
            cut = cut if cut > cap // 2 else cap
            parts.append(para[:cut].rstrip()); para = para[cut:].lstrip()
        cur = para
    if cur:
        parts.append(cur)
    return parts or [""]


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    dry = not a.apply
    cfg = cp.load_config(Path(a.config))
    if not cfg.get("publish_dir") or not cfg.get("publish_layers"):
        print("publisher is off (set publish_dir and publish_layers in comms.toml); nothing done"); return
    vault = Path(cfg["publish_dir"]).expanduser()
    layers = dict(item.split("=", 1) for item in cfg["publish_layers"])
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    state_path = Path(cfg.get("publish_state") or (CC / "state" / "publish.json"))
    state = load_state(state_path)

    # ---- files in scope -----------------------------------------------------------------------
    files = {}
    for folder in layers:
        root = vault / folder
        if not root.is_dir():
            print(f"missing folder {root}"); continue
        for p in sorted(root.rglob("*.md")):
            rel = str(p.relative_to(vault))
            raw = p.read_text(encoding="utf-8", errors="replace")
            files[rel] = {"folder": folder, "text": canonical(raw), "stem": p.stem, "parent": p.parent.name}
    # titles: stem, disambiguated by parent folder when a stem repeats
    seen = {}
    for rel, f in files.items():
        seen.setdefault(f["stem"], []).append(rel)
    for rel, f in files.items():
        f["title"] = f"Note: {f['stem']}" + (f" ({f['parent']})" if len(seen[f["stem"]]) > 1 else "")

    # ---- calendar-side view: everything this publisher wrote, plus every key in use ------------
    events, page = [], None
    while True:
        r = svc.events().list(calendarId=cal, timeMin="2999-12-01T00:00:00Z", timeMax="9999-01-01T00:00:00Z", singleEvents=True,
                              maxResults=2500, pageToken=page).execute()
        events += r.get("items", []); page = r.get("nextPageToken")
        if not page:
            break
    taken = {t for e in events for t in (e.get("location") or "").split() if links.is_key(t)}
    taken |= {v["key"] for v in state.values() if v.get("key")}
    mine = {e["id"]: e for e in events if e.get("extendedProperties", {}).get("private", {}).get("comms_writer") == WRITER}

    # ---- pass 1: identity for every file (rename = same hash at a new path) -------------------
    by_hash = {v["hash"]: rel for rel, v in state.items() if rel not in files}
    for rel, f in files.items():
        f["hash"] = h(f["text"])
        if rel not in state:
            old = by_hash.pop(f["hash"], None)
            if old:
                state[rel] = state.pop(old); print(("[dry] " if dry else "") + f"rename  {old} -> {rel}")
            else:
                state[rel] = {"key": links.mint(taken), "event_ids": [], "hash": None}
    name2key = {f["stem"]: state[rel]["key"] for rel, f in files.items()}
    for e in events:      # the two-way mirror's notes are link targets too
        k = links.key_of(e.get("description") or "")
        if k:
            name2key.setdefault(re.sub(r"^Note:?\s*", "", e["summary"]).strip(), k)

    # ---- pass 2: expand, measure, split, publish ------------------------------------------------
    counts = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0, "split": 0, "overwrote calendar edit": 0}
    missing = set()
    by_folder_titles = {}
    for rel, f in files.items():
        st_ = state[rel]; key = st_["key"]
        expanded = links.up(f["text"], name2key, missing)
        parts = split_parts(expanded, BODY_CAP)
        if len(parts) > 1:
            counts["split"] += 1; print(f"split   {rel}: {len(expanded)} expanded chars -> {len(parts)} parts")
        day = f"{layers[f['folder']]}-01-01"
        titles = [f["title"] if len(parts) == 1 else f"{f['title']} · part {i + 1} of {len(parts)}" for i in range(len(parts))]
        by_folder_titles.setdefault(f["folder"], []).append((f["title"], day, len(parts)))
        bodies = [p.rstrip() + "\n\n" + key + "\n" for p in parts]
        ids = list(st_.get("event_ids") or [])
        # trim or grow the event set to the part count
        for eid in ids[len(parts):]:
            print(("[dry] " if dry else "") + f"delete  extra part of {rel}")
            if not dry:
                cp.pace(); svc.events().delete(calendarId=cal, eventId=eid).execute()
            counts["deleted"] += 1
        ids = ids[:len(parts)]
        for i, (title, body) in enumerate(zip(titles, bodies)):
            priv = {"comms_kind": "note", "comms_writer": WRITER, "publish_path": rel, "publish_hash": f["hash"], "publish_part": str(i + 1)}
            ev_body = {"summary": title, "start": {"date": day}, "end": {"date": date.fromisoformat(day).replace(day=2).isoformat()},
                       "location": key, "description": body, "extendedProperties": {"private": priv}}
            cp.check_cap(body, title)
            if i < len(ids) and ids[i] in mine:
                cur = mine[ids[i]]
                same = (cur.get("summary") == title and (cur.get("description") or "") == body and (cur["start"].get("date") == day))
                if same:
                    counts["unchanged"] += 1; continue
                if st_.get("hash") == f["hash"] and (cur.get("description") or "") != body:
                    counts["overwrote calendar edit"] += 1
                print(("[dry] " if dry else "") + f"update  {title}")
                if not dry:
                    cp.write_event(svc, cal, ids[i], ev_body, replace_meta=True)
                counts["updated"] += 1
            else:
                print(("[dry] " if dry else "") + f"insert  {title} ({day})")
                if not dry:
                    ev = cp.insert_event(svc, cal, ev_body)
                    if i < len(ids):
                        ids[i] = ev["id"]
                    else:
                        ids.append(ev["id"])
                counts["inserted"] += 1
        st_["event_ids"] = ids; st_["hash"] = f["hash"]; st_["title"] = f["title"]

    # ---- deletions: files gone from the vault -------------------------------------------------
    for rel in [r for r in state if r not in files]:
        for eid in state[rel].get("event_ids") or []:
            print(("[dry] " if dry else "") + f"delete  {state[rel].get('title', rel)} (file gone)")
            if not dry and eid in mine:
                cp.pace(); svc.events().delete(calendarId=cal, eventId=eid).execute()
            counts["deleted"] += 1
        del state[rel]

    # ---- one dated index note per folder --------------------------------------------------------
    for folder, rows in by_folder_titles.items():
        title = f"Note: {folder} index"
        lines = [f"Index of the {folder} folder, published one-way from Obsidian ({len(rows)} notes). "
                 f"Open one by searching its exact title with the day pinned to the date shown; the calendar copy is read-only, "
                 "edits belong in Obsidian.", ""]
        lines += [f"- {t} ({d})" + (f" · {n} parts" if n > 1 else "") for t, d, n in sorted(rows)]
        lines += ["", f"Updated {date.today().isoformat()}"]
        body = "\n".join(lines)
        cur = next((e for e in mine.values() if e.get("summary") == title), None)
        if cur and (cur.get("description") or "") == body:
            continue
        ev_body = {"summary": title, "start": {"date": INDEX_DAY}, "end": {"date": "3000-01-03"}, "description": body,
                   "extendedProperties": {"private": {"comms_kind": "note", "comms_writer": WRITER, "publish_path": f"{folder}/"}}}
        cp.check_cap(body, title)
        print(("[dry] " if dry else "") + f"{'update' if cur else 'insert'}  {title}")
        if not dry:
            if cur:
                cp.write_event(svc, cal, cur["id"], ev_body, replace_meta=True)
            else:
                cp.insert_event(svc, cal, ev_body)

    for e in mine.values():   # an index whose folder no longer publishes anything
        pp = e.get("extendedProperties", {}).get("private", {}).get("publish_path", "")
        if pp.endswith("/") and pp[:-1] not in by_folder_titles:
            print(("[dry] " if dry else "") + f"delete  {e.get('summary')} (folder empty)")
            if not dry:
                cp.pace(); svc.events().delete(calendarId=cal, eventId=e["id"]).execute()
    if not dry:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print(("[dry] " if dry else "") + f"publish pass: {len(files)} files, {counts}"
          + (f"; {len(missing)} link targets without a key stayed name-only" if missing else ""))


if __name__ == "__main__":
    main()
