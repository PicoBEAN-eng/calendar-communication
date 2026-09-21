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
from datetime import date, datetime, timezone, timedelta
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


FRONTMATTER_KEYS = False   # comms.toml mirror_frontmatter_keys: vault-side identity in YAML frontmatter
                           # (key:, parent:) instead of a trailing key line (operator 2026-09-22); the
                           # calendar side is unchanged either way. Default off: Frames' notes untouched.


def canonical(text: str) -> str:
    """The comparable content: no vault-only fences, no frontmatter, no trailing key line. Identity travels
    beside the content (last line on the calendar, frontmatter or last line in the vault)."""
    _, body = links.frontmatter(text or "")
    return links.strip_key_line(FENCE.sub("", body)).strip()


# Link registry (name <-> identity key) over every mirrored note; filled by main() before any sync.
REG = {"name2key": {}, "key2name": {}, "missing": set()}


def register(ev, rel: str):
    key = links.key_of(ev.get("description") or "")
    if not key:
        return
    for name in (re.sub(r"^Note:\s*", "", ev.get("summary", "")).strip(), Path(rel).stem):
        REG["name2key"][name] = key
    REG["key2name"][key] = Path(rel).stem      # links resolve by FILE name in Obsidian (":" etc. are unsafe there)


PART_MARK = "%% part %%"
PART_TITLE = re.compile(r" · part (\d+) of (\d+)$")


def split_parts(canon: str):
    """A note becomes several calendar events when it carries part markers (the operator's or an
    emitter's carving, never re-flowed) or when it is over the cap (mechanical paragraph split).
    Returns (parts, mode) with mode in 'marker' | 'auto' | None."""
    if re.search(r"(?m)^" + re.escape(PART_MARK) + r"\s*$", canon):
        parts = [p.strip() for p in re.split(r"(?m)^" + re.escape(PART_MARK) + r"\s*$", canon)]
        return [p for p in parts if p], "marker"
    if len(canon) <= CAP:
        return [canon], None
    parts, cur = [], ""
    for para in canon.split("\n\n"):
        piece = (cur + "\n\n" + para) if cur else para
        if len(piece) <= CAP - 20:
            cur = piece; continue
        if cur:
            parts.append(cur); cur = ""
        while len(para) > CAP - 20:
            cut = max(para.rfind("\n", 0, CAP - 20), para.rfind(" ", 0, CAP - 20)); cut = cut if cut > CAP // 2 else CAP - 20
            parts.append(para[:cut].rstrip()); para = para[cut:].lstrip()
        cur = para
    if cur:
        parts.append(cur)
    return parts, "auto"


def join_parts(parts: list, mode: str | None, key: str | None) -> str:
    """Inverse of split_parts on the calendar side: parts carry the key line at the end of each event
    for search; only the last one keeps it in the vault text."""
    texts = []
    for i, t in enumerate(parts):
        t = t.strip()
        if key and i < len(parts) - 1 and links.key_of(t) == key:
            t = t[: t.rstrip().rfind(key)].rstrip()
        texts.append(t)
    sep = f"\n\n{PART_MARK}\n\n" if mode == "marker" else "\n\n"
    return sep.join(texts)


def base_title(summary: str) -> str:
    return PART_TITLE.sub("", summary or "")


def group_canonical(group: list) -> str:
    """The vault-form text of a note held as one or more calendar events (sorted by part)."""
    primary = group[0]
    mode = primary.get("extendedProperties", {}).get("private", {}).get("mirror_split") or None
    texts = [links.down(canonical(e.get("description") or ""), REG["key2name"]) for e in group]
    key = links.key_of(texts[-1])
    return join_parts(texts, mode, key) if len(texts) > 1 else texts[0]


def fences(text: str) -> str:
    return "".join(m.group(0) for m in FENCE.finditer(text))


def h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def file_name(title: str) -> str:
    return UNSAFE.sub("-", re.sub(r"^Note:?\s*", "", title)).strip() + ".md"


def stable(path: Path) -> bool:
    return (datetime.now().timestamp() - path.stat().st_mtime) >= STABLE_SECONDS


def write_file(path: Path, canon: str, keep_fences: str, dry: bool, key: str | None = None, parent: str | None = None):
    """The vault copy: content, then the surviving fences; identity as frontmatter (key, parent) when
    mirror_frontmatter_keys is on, else the key on the last line. Other frontmatter properties already in
    the file are carried forward."""
    body = canon.rstrip() + ("\n\n" + keep_fences.strip() + "\n" if keep_fences.strip() else "\n")
    if key:
        if FRONTMATTER_KEYS:
            props = links.frontmatter(path.read_text(encoding="utf-8", errors="replace"))[0] if path.exists() else {}
            body = links.with_frontmatter(body, key, parent, {k: v for k, v in props.items() if k not in ("key", "parent")})
        else:
            body = body.rstrip() + "\n\n" + key + "\n"
    print(("[dry] " if dry else "") + f"vault  <- calendar  {path.name}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def write_event(svc, cal, group, canon: str, dry: bool, key: str | None = None):
    """Write a note's vault-form text to its calendar event(s): parts are created, updated and
    trimmed to match; every part carries the key line for search; titles say part n of m.
    The key comes from the caller or the primary event's text (never from canon, which is identity-free)."""
    parts, mode = split_parts(canon)
    primary = group[0]
    key = key or links.key_of(primary.get("description") or "")
    title = primary.get("summary") or ""
    if primary.get("extendedProperties", {}).get("private", {}).get("mirror_part"):
        title = base_title(title)       # a hand-parted title ("· part 1 of 2" as its own note) is left alone
    m = len(parts)
    bodies = []
    for i, part in enumerate(parts):
        body = links.up(part, REG["name2key"], REG["missing"])
        if key and links.key_of(body) != key:          # every calendar part carries the key line
            body = body.rstrip() + "\n\n" + key + "\n"
        if len(body) > cp.DESCRIPTION_CAP - 8:
            print(f"SKIP {title}: part {i + 1} is {len(body)} chars, over the cap even after splitting (flagged, never truncated)")
            return False
        bodies.append(body)
    print(("[dry] " if dry else "") + f"calendar <- vault  {title}" + (f" ({m} parts)" if m > 1 else ""))
    if dry:
        return True
    priv_base = {k: v for k, v in (primary.get("extendedProperties", {}).get("private") or {}).items()
                 if k not in ("mirror_part", "mirror_parts", "mirror_split", "mirror_hash")}
    for i, body in enumerate(bodies):
        summary = title + (f" · part {i + 1} of {m}" if m > 1 else "")
        priv = {"mirror_part": str(i + 1), "mirror_parts": str(m), "mirror_split": mode or ""}
        if i < len(group):
            cp.write_event(svc, cal, group[i]["id"], {"summary": summary, "description": body,
                           "extendedProperties": {"private": priv}}, existing=group[i], verify=False)
            group[i]["summary"] = summary; group[i]["description"] = body
        else:
            ev = cp.insert_event(svc, cal, {"summary": summary, "start": primary["start"], "end": primary["end"],
                                            "location": primary.get("location", ""), "description": body,
                                            "extendedProperties": {"private": {**priv_base, **priv}}})
            group.append(ev)
    for extra in group[m:]:
        cp.pace(); svc.events().delete(calendarId=cal, eventId=extra["id"]).execute()
    del group[m:]
    return True


def set_memory(svc, cal, group, rel: str, hsh: str, dry: bool):
    for ev in (group if isinstance(group, list) else [group]):
        if not dry:
            cp.write_event(svc, cal, ev["id"], {"extendedProperties": {"private": {"mirror_path": rel, "mirror_hash": hsh}}}, existing=ev, verify=False)
        ev.setdefault("extendedProperties", {}).setdefault("private", {}).update({"mirror_path": rel, "mirror_hash": hsh})


def conflict_save(vault: Path, rel: str, loser_text: str, who: str, dry: bool):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = vault / "_conflicts" / f"{Path(rel).stem} · {stamp} · {who}.md"
    print(("[dry] " if dry else "") + f"conflict: {who} version kept at _conflicts/{p.name}")
    if not dry:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(loser_text, encoding="utf-8")


def mirror_one(svc, cal, vault: Path, group, rel: str, dry: bool):
    if not isinstance(group, list):
        group = [group]
    ev = group[0]
    path = vault / rel
    priv = ev.get("extendedProperties", {}).get("private", {})
    last = priv.get("mirror_hash")
    e_canon = group_canonical(group)   # calendar form (parts, glued keys) -> vault form
    key = links.key_of(ev.get("description") or "")
    parent = st.parse_location(ev.get("location"), key)[1]
    if not path.exists():
        write_file(path, e_canon, "", dry, key, parent)
        set_memory(svc, cal, group, rel, h(e_canon), dry)
        return "created in vault"
    if not stable(path):
        return "skipped: file changed in the last 30 s (Syncthing may be mid-write)"
    raw = path.read_text(encoding="utf-8")
    f_canon, keep = canonical(raw), fences(raw)
    if e_canon == f_canon:
        if last != h(e_canon):
            set_memory(svc, cal, group, rel, h(e_canon), dry)
        return "in sync"
    he, hf = h(e_canon), h(f_canon)
    if hf == last and he != last:
        write_file(path, e_canon, keep, dry, key, parent); set_memory(svc, cal, group, rel, he, dry); return "calendar -> vault"
    if he == last and hf != last:
        if write_event(svc, cal, group, f_canon, dry):
            set_memory(svc, cal, group, rel, hf, dry)
        return "vault -> calendar"
    # both moved: last writer wins, loser kept
    ev_t = datetime.fromisoformat(ev["updated"].replace("Z", "+00:00"))
    f_t = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if f_t >= ev_t:
        conflict_save(vault, rel, e_canon, "calendar", dry)
        if write_event(svc, cal, group, f_canon, dry):
            set_memory(svc, cal, group, rel, hf, dry)
        return "conflict: vault won"
    conflict_save(vault, rel, raw, "vault", dry)
    write_file(path, e_canon, keep, dry, key, parent); set_memory(svc, cal, group, rel, he, dry)
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


def band_floor(cfg=None) -> str:
    """The structural band starts a month before the anchor year (derived from note_anchor_date, never
    hard-coded: the band moved from 3000 to 2040 on 2026-09-22); the ceiling stays 9999 so the traffic
    and transcript bands are in view."""
    try:
        y = int(str((cfg or {}).get("note_anchor_date", "3000-01-01"))[:4])
    except ValueError:
        y = 3000
    return f"{y - 1}-12-01T00:00:00Z"


def list_future(svc, cal, cfg=None):
    items, page = [], None
    while True:
        r = svc.events().list(calendarId=cal, timeMin=band_floor(cfg), timeMax="9999-01-01T00:00:00Z", singleEvents=True,
                              maxResults=2500, pageToken=page).execute()
        items += r.get("items", []); page = r.get("nextPageToken")
        if not page:
            return [e for e in items if re.match(r"^Note:?\s", e.get("summary", ""))]


GROUPS = {}     # identity key -> [events], all parts of a note (filled by main)


def set_cache(svc, cal, n, rel, dry, vault=None):
    priv = {"mirror_path": rel}
    if n.is_folder and not st.FOLDER_FILES and vault is not None:
        d = vault / Path(rel).parent
        if d.is_dir():
            stt = d.stat(); priv["mirror_inode"] = f"[{stt.st_dev}, {stt.st_ino}]"
    for ev in GROUPS.get(n.key) or [n.ev]:      # every part of the note carries the same path
        if not dry:
            cp.write_event(svc, cal, ev["id"], {"extendedProperties": {"private": priv}}, existing=ev, verify=False)
        ev.setdefault("extendedProperties", {}).setdefault("private", {}).update(priv)
    n.cache = rel


def rename_folder(svc, cal, n, new_name, dry):
    print(("[dry] " if dry else "") + f"token   <- vault   {n.name}: folder renamed -> {new_name}")
    if not dry:
        cp.write_event(svc, cal, n.ev["id"], {"summary": f"Note: {new_name}"}, existing=n.ev, verify=False)
    n.ev["summary"] = f"Note: {new_name}"


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
        if n.is_folder and not st.FOLDER_FILES:      # a directory, no file: move the directory itself
            dst.parent.parent.mkdir(parents=True, exist_ok=True)
            if src.parent.exists():
                src.parent.rename(dst.parent)
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


def init_structure(svc, cal, vault, nodes, dry, cfg=None):
    cfg = cfg or {}
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
            fday = st.folder_day(cfg)
            ev = {"summary": f"Note: {name}", "start": {"date": fday}, "end": {"date": (date.fromisoformat(fday) + timedelta(days=1)).isoformat()},
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


def auto_adopt(svc, cal, events, cfg, dry) -> int:
    known = {}
    for e in events:
        priv = e.get("extendedProperties", {}).get("private", {})
        if priv.get("mirror_path") and priv.get("comms_kind") == "folder":
            k = links.key_of(e.get("description") or "")
            if k:
                known[k] = str(Path(priv["mirror_path"]).parent)      # folder key -> its directory
    if not known:
        return 0
    taken = {t for e in events for t in (e.get("location") or "").split() if links.is_key(t)}
    taken |= {links.key_of(e.get("description") or "") for e in events} - {None}
    n = 0
    for e in sorted(events, key=lambda e: e.get("created", "")):
        priv = e.get("extendedProperties", {}).get("private", {})
        if priv.get("mirror_path") or priv.get("comms_kind") in ("cast",):
            continue
        title = e.get("summary") or ""
        _, parent, classes, others = st.parse_location(e.get("location"), links.key_of(e.get("description") or ""))
        if not parent or parent not in known:
            continue
        is_folder = title.rstrip().endswith("/")
        name = st.title_name(title.rstrip().rstrip("/"))
        key = links.key_of(e.get("description") or "") or links.mint(taken)
        body = {}
        if key != links.key_of(e.get("description") or ""):
            body["description"] = links.with_key(e.get("description") or "", key)
        body["location"] = st.format_location(key, parent, classes, others)
        rel = f"{known[parent]}/{name}/{name}.md" if is_folder else f"{known[parent]}/{name}.md"
        priv_new = {"mirror_path": rel, "comms_kind": "folder" if is_folder else priv.get("comms_kind", "note")}
        if is_folder:
            body["summary"] = title.rstrip().rstrip("/").rstrip()
            known[key] = f"{known[parent]}/{name}"
        body["extendedProperties"] = {"private": priv_new}
        print(("[dry] " if dry else "") + f"adopt   {'folder ' if is_folder else ''}{title} -> {rel} (key {key}, parent {parent})")
        if not dry:
            cp.write_event(svc, cal, e["id"], body, existing=e, verify=False)
        e.update({k: v for k, v in body.items() if k != "extendedProperties"})
        e.setdefault("extendedProperties", {}).setdefault("private", {}).update(priv_new)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--vault")
    ap.add_argument("--adopt", help="exact title to bring under the mirror")
    ap.add_argument("--layer", help="YYYY-MM-DD of the note's layer day (with --adopt)")
    ap.add_argument("--folder", help="vault folder for that layer (with --adopt)")
    ap.add_argument("--init-structure", action="store_true", help="one-time: folder-notes + parent tokens from the cached paths")
    ap.add_argument("--restamp", action="store_true", help="rewrite every mirrored vault file's identity in the current style (frontmatter or key line); no calendar writes")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    if not (a.vault or cfg.get("mirror_dir")):
        print("mirror is off for this instance (set mirror_dir in comms.toml); nothing done"); return
    vault = Path(a.vault or cfg["mirror_dir"]).expanduser()
    st.EXCLUDE[:] = [x.strip("/") for x in (cfg.get("mirror_exclude") or [])]
    st.FOLDER_FILES = bool(cfg.get("mirror_folder_notes", True))
    global FRONTMATTER_KEYS
    FRONTMATTER_KEYS = bool(cfg.get("mirror_frontmatter_keys", False))
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    events = list_future(svc, cal, cfg)
    if a.adopt:
        ev = next((e for e in events if e.get("summary") == a.adopt and (e["start"].get("date") or e["start"]["dateTime"][:10]) == a.layer), None)
        if not ev:
            sys.exit(f"no event titled {a.adopt!r} on {a.layer}")
        ev.setdefault("extendedProperties", {}).setdefault("private", {})["mirror_path"] = f"{a.folder}/{file_name(ev['summary'])}"
        body = {"extendedProperties": {"private": {"mirror_path": ev["extendedProperties"]["private"]["mirror_path"]}}}
        # identity: a note enters the mirror with its key on the last line and in location (the structure
        # pass only sees keyed notes); an event that already carries one keeps it
        key = links.key_of(ev.get("description") or "")
        if not key:
            taken = {t for e in events for t in (e.get("location") or "").split() if links.is_key(t)}
            taken |= {links.key_of(e.get("description") or "") for e in events} - {None}
            key = links.mint(taken)
            body["description"] = links.with_key(ev.get("description") or "", key)
            body["location"] = key
            ev["description"] = body["description"]; ev["location"] = cp.merge_location(ev.get("location") or "", key)
            print(("[dry] " if a.dry_run else "") + f"adopt   {a.adopt}: key {key} minted")
        if not a.dry_run:
            cp.write_event(svc, cal, ev["id"], body, existing=ev, verify=False)

    # ---- calendar-born notes (2026-09-22): a "Note:" event on the band with a p-token naming a known
    # folder-note is adopted automatically: key minted if missing, mirror_path derived from the parent
    # chain; a title ending in "/" is a new folder-note (its file sits inside its own folder). So the
    # phone authors a note or a folder by writing a title and a parent token in the location field.
    adopted = auto_adopt(svc, cal, events, cfg, a.dry_run)
    if adopted:
        print(f"{'[dry] ' if a.dry_run else ''}auto-adopted {adopted} calendar-born note(s)")

    # ---- structure pass: parent tokens are the truth, paths are derived ------------------------
    def part_no(e):
        return int(e.get("extendedProperties", {}).get("private", {}).get("mirror_part") or 1)
    groups = {}
    for e in events:
        rel_ = e.get("extendedProperties", {}).get("private", {}).get("mirror_path")
        if rel_:
            groups.setdefault(rel_, []).append(e)
    for g in groups.values():
        g.sort(key=part_no)
        for e in g:      # structure and links see a parted note under its base title
            if e.get("extendedProperties", {}).get("private", {}).get("mirror_part"):
                e["summary"] = base_title(e.get("summary"))
    nodes = st.build([g[0] for g in groups.values()])
    nodes = {k: n for k, n in nodes.items() if n.cache}      # only mirrored notes have positions
    for n in nodes.values():
        GROUPS[n.key] = groups.get(n.cache, [n.ev])
    for n in nodes.values():
        TAKEN.add(n.key)
    if a.init_structure:
        init_structure(svc, cal, vault, nodes, a.dry_run, cfg)
    problems = st.derive(nodes)
    actual = st.scan_vault(vault, {k: n.cache for k, n in nodes.items()})
    actions = st.reconcile(nodes, vault, actual, print)
    for kind, n, arg in actions:
        if kind == "move-file":
            move_file(vault, n, arg, a.dry_run); set_cache(svc, cal, n, arg, a.dry_run, vault)
        elif kind == "set-parent":
            set_parent(svc, cal, n, arg, a.dry_run); set_cache(svc, cal, n, n.actual, a.dry_run, vault)
        elif kind == "rename":
            rename_folder(svc, cal, n, arg, a.dry_run)
        elif kind == "cache":
            set_cache(svc, cal, n, arg, a.dry_run, vault)
    if actions:
        problems = st.derive(nodes)          # parents and names may have changed: re-derive before listings
        for n in nodes.values():
            if not n.pinned and n.derived and n.cache != n.derived:
                set_cache(svc, cal, n, n.derived, a.dry_run, vault)
    for n, why in problems:
        print(f"structure: {n.name}: {why}")

    targets = []
    for n in nodes.values():
        g = groups.get(n.ev["extendedProperties"]["private"].get("mirror_path"), [n.ev])
        g = g if g and g[0] is n.ev else [n.ev] + [e for e in g if e is not n.ev]
        targets.append((g, n.cache))
    for g, rel in targets:
        register(g[0], rel)

    # ---- folder listings: generated on the nexus, written to both sides ------------------------
    for n in nodes.values():
        if not n.is_folder or n.pinned:
            continue
        gen = st.listing(n, nodes)
        path = vault / n.cache
        if not st.FOLDER_FILES:
            # a folder is a directory and nothing else: the listing lives on the event only
            d = path.parent
            if not d.is_dir():
                print(("[dry] " if a.dry_run else "") + f"mkdir   {d.relative_to(vault)}")
                if not a.dry_run:
                    d.mkdir(parents=True, exist_ok=True)
                    set_cache(svc, cal, n, n.cache, a.dry_run, vault)      # records the directory's inode
            current = n.ev.get("description") or ""
            new = st.with_listing(canonical(current), gen, n.key)
            if canonical(new) != canonical(current):
                print(("[dry] " if a.dry_run else "") + f"listing -> event   {n.name}")
                if not a.dry_run:
                    cp.write_event(svc, cal, n.ev["id"], {"description": links.up(new, REG["name2key"], REG["missing"]),
                                   "extendedProperties": {"private": {"mirror_hash": h(new)}}}, existing=n.ev, verify=False)
                n.ev["description"] = new
            continue
        current = path.read_text(encoding="utf-8") if path.exists() else (n.ev.get("description") or "")
        keep = fences(current)
        new = st.with_listing(canonical(current), gen, n.key)
        if canonical(new) != canonical(current) or not path.exists():
            print(("[dry] " if a.dry_run else "") + f"listing -> both    {n.name}")
            write_file(path, canonical(new), keep, a.dry_run, n.key, n.parent)
            if not a.dry_run:
                cp.write_event(svc, cal, n.ev["id"], {"description": links.up(new, REG["name2key"], REG["missing"]),
                               "extendedProperties": {"private": {"mirror_hash": h(new)}}}, existing=n.ev, verify=False)
            n.ev["description"] = new
            n.ev.setdefault("extendedProperties", {}).setdefault("private", {})["mirror_hash"] = h(new)

    if a.restamp:
        n_ = 0
        for g, rel in targets:
            path = vault / rel
            if not path.exists() or rel in {n.cache for n in nodes.values() if n.is_folder and not st.FOLDER_FILES}:
                continue
            raw = path.read_text(encoding="utf-8")
            key = links.key_of(g[0].get("description") or ""); parent = st.parse_location(g[0].get("location"), key)[1]
            write_file(path, canonical(raw), fences(raw), a.dry_run, key, parent); n_ += 1
        print(("[dry] " if a.dry_run else "") + f"restamped {n_} vault files ({'frontmatter' if FRONTMATTER_KEYS else 'key line'})")

    # ---- content pass --------------------------------------------------------------------------
    changed = 0
    dir_only = {n.cache for n in nodes.values() if n.is_folder and not st.FOLDER_FILES}
    for g, rel in targets:
        if rel in dir_only:
            continue          # a directory, no file to mirror
        res = mirror_one(svc, cal, vault, g, rel, a.dry_run)
        if res != "in sync":
            print(f"{g[0]['summary']}: {res}")
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
