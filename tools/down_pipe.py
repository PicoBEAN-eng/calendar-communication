#!/usr/bin/env python3
"""Down-pipe: calendar -> vault, TICKS ONLY (spec: "Note: Down-pipe on Active and vault revert", 2026-09-19).

The calendar never writes prose into the vault. Each pass reads every published note in the allowed layers
back from the calendar, translates its links down, and diffs it line by line against the CURRENT vault file.
Exactly three shapes of difference are accepted, everything else is rejected (reported, journaled, and then
overwritten by the next publish pass):
  1. tick    — a vault line "- [ ] …" whose calendar copy is "- [x] …", otherwise identical
  2. number  — a vault line containing a blank (___ or a lone _) whose calendar copy has digits in that place
  3. note    — a new non-empty line under a Notes heading (or text typed over the "- " placeholder there)
The vault always wins on content: an accepted change is applied to the vault file as it is NOW, by matching
the vault line's text; a tick on a line the vault no longer has is dropped and reported. Nothing is written
unless down_pipe = "apply"; "log" runs the whole diff and journals what WOULD happen (phase A).

Scope fences: only folders whose layer year is in down_pipe_layers; the target must resolve (realpath, no
symlinks) inside one of those folders; only the three shapes; at most down_pipe_max_lines per pass. Before
the first write of a pass, down_pipe_snapshot_cmd runs (the vault git snapshot) and its commit is journaled;
every applied line is journaled with its before/after text. Journal: down_pipe_journal (in the vault, so it
is snapshotted with everything else).

comms.toml:
    down_pipe = "off" | "log" | "apply"        (default off)
    # Scope (2026-09-24): the folder MODE on each publish_layers line — `path=year|label|mode`. interactive =
    # the shape grammar applies (this file's three shapes + table rows); freeform = any line edit; readonly =
    # never read, the publisher re-asserts its copy. When no folder carries a mode, the older fences below apply:
    down_pipe_layers = ["3060"]
    down_pipe_folders = ["! Active/Location Checks"]   # optional: narrow the whole scope to these folder paths or keys
    down_pipe_apply_folders = ["! Active/Location Checks"]   # optional: with "apply", only these folders write; the rest of the scope logs
    down_pipe_max_lines = 50
    down_pipe_journal = "/path/to/vault/The Warehouse/Records/Down-pipe log.md"
    down_pipe_snapshot_cmd = "/home/x/woolly-workplace/bin/vault-snapshot"

  down_pipe.py [--mode off|log|apply] [--dry-run] [--config comms.toml]      exit 0; 3 = something was applied
"""
import argparse, difflib, html, json, os, re, subprocess, sys
from datetime import datetime
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402
import links  # noqa: E402
import publish_up as pub  # noqa: E402

HTML_BREAK = re.compile(r"<\s*(?:br|/p|/div|/li|/tr)\s*/?\s*>", re.I)
HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")   # real tags only: <b>, </p>, <a href=…>; never <name@host> or <https://…>


def normalise_html(text: str) -> str:
    """The calendar app stores an edited description as HTML: <br>/<p> for line breaks, entities, autolinked
    URLs. Bring it back to the plain text the publisher wrote so the line diff sees only the edit. A body with
    no real tags is returned untouched (angle-bracket emails and URLs in the source are not tags)."""
    # Only a body the calendar app has HTML-ified (break/paragraph tags or a closing tag) is normalised; a lone
    # placeholder like `<ref>` in a note's own text is not a tag and must survive untouched.
    if not (HTML_BREAK.search(text or "") or re.search(r"</[a-zA-Z][a-zA-Z0-9-]*>", text or "")):
        return text
    t = HTML_BREAK.sub("\n", text)
    t = HTML_TAG.sub("", t)
    t = html.unescape(t).replace("\xa0", " ")
    return "\n".join(ln.rstrip() for ln in t.splitlines())


TICK_OPEN = re.compile(r"^(\s*[-*]\s+)\[ \](.*)$")
TICK_DONE = re.compile(r"^(\s*[-*]\s+)\[[xX]\](.*)$")
BLANK = re.compile(r"_{2,}|(?<![\w_])_(?![\w_])")
NUMBER = re.compile(r"\d+")
NOTES_HEAD = re.compile(r"^#{1,6}\s*(?:\d+\s*·\s*)?Notes\b", re.I)
HEAD = re.compile(r"^#{1,6}\s")
PLACEHOLDER = re.compile(r"^\s*[-*]\s*$")
HTML_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")


def classify(v: str, c: str, freeform: bool = False):
    """(kind, None) for an accepted line pair, else (None, reason). In free-form folders (2026-09-22,
    operator: the ticks-only grammar is a rail for shaky hands, not needed where a careful author writes)
    any change to a line is accepted; the three shapes still classify so the journal keeps saying which."""
    if v == c:
        return "same", None
    mo, mc = TICK_OPEN.match(v), TICK_DONE.match(c)
    if mo and mc and mo.group(1) == mc.group(1):
        if mo.group(2) == mc.group(2):
            return "tick", None
        # tick AND a number on the same line
        if (BLANK.search(mo.group(2)) or TRAILING_FIELD.search(mo.group(2))) and blank_to_number(mo.group(2), mc.group(2)):
            return "tick+number", None
        return ("edit", None) if freeform else (None, "ticked line also changed its text")
    if (BLANK.search(v) or TRAILING_FIELD.search(v)) and blank_to_number(v, c):
        return "number", None
    if freeform:
        return "edit", None
    return None, "text changed (not a tick, a number over a blank, or a note)"


TRAILING_FIELD = re.compile(r":\s*$")


def blank_to_number(v: str, c: str) -> bool:
    """True iff c equals v with every blank replaced by digits (at least one blank filled). A line ending in a
    bare field ("… · actual:", the 2026-09-19 sheet form) counts as one blank at its end."""
    if TRAILING_FIELD.search(v) and not BLANK.search(v):
        return bool(re.match("^" + re.escape(v.rstrip()) + r"\s*\d+\s*$", c))
    pat = "^" + "".join(re.escape(seg) if i % 2 == 0 else r"(\d+|_+)"
                        for i, seg in enumerate(re.split(r"(_{2,}|(?<![\w_])_(?![\w_]))", v))) + "$"
    m = re.match(pat, c)
    return bool(m) and any(g.isdigit() for g in m.groups())


def section_is_notes(lines: list, idx: int) -> bool:
    for j in range(idx, -1, -1):
        if HEAD.match(lines[j]):
            return bool(NOTES_HEAD.match(lines[j]))
    return False


def read_calendar_copy(svc, cal, entry, key2name) -> str | None:
    parts = []
    for eid in entry.get("event_ids") or []:
        cp.pace()
        try:
            ev = svc.events().get(calendarId=cal, eventId=eid).execute()
        except Exception as e:  # noqa: BLE001
            print(f"  cannot read event {eid}: {e}"); return None
        body = normalise_html(ev.get("description") or "").rstrip()
        # The identity key is metadata, not content, and a phone edit that appends text pushes it off the last
        # line, so it is stripped wherever it sits (links.strip_key_line, shared with the mirror since d0b57d8).
        parts.append(links.strip_key_line(body).rstrip())
    if not parts:
        return None
    return links.down("\n\n".join(parts), key2name)


def diff_note(vault_text: str, cal_text: str, freeform: bool = False):
    """Returns (accepted: [dict], rejected: [dict]). Blank lines are ignored on both sides;
    a calendar line that duplicates an existing vault line (repeated table headers) is ignored."""
    # Machine markers (`<!-- packer box 1 -->` on the order pages) are invisible to the diff on both sides:
    # the calendar app strips HTML comments when a phone edit HTML-ifies the body, and a free-form pass must
    # never read that as a delete (2026-09-23, the SY air page joining the free-form set).
    V = [ln.rstrip() for ln in vault_text.splitlines() if ln.strip() and not HTML_COMMENT.match(ln)]
    C = [ln.rstrip() for ln in cal_text.splitlines() if ln.strip() and not HTML_COMMENT.match(ln)]
    vset = set(V)
    acc, rej = [], []
    sm = difflib.SequenceMatcher(a=V, b=C, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        vs, cs = V[i1:i2], C[j1:j2]
        if op == "replace" and len(vs) == len(cs):
            for v, c in zip(vs, cs):
                kind, why = classify(v, c, freeform)
                if kind in ("tick", "number", "tick+number", "edit"):
                    acc.append({"kind": kind, "before": v, "after": c})
                elif PLACEHOLDER.match(v) and section_is_notes(V, V.index(v)) and c.strip():
                    acc.append({"kind": "note", "before": v, "after": c})
                elif kind is None:
                    rej.append({"before": v, "after": c, "why": why})
            continue
        if op == "insert":
            prev = V[i1 - 1] if i1 > 0 else None
            for c in cs:
                if c in vset:
                    continue      # repeated table header from the part split, or an echo of an existing line
                if freeform or section_is_notes(C, C.index(c)):
                    acc.append({"kind": "note" if not freeform else "insert", "before": None, "after": c, "anchor": prev})
                    prev = c
                else:
                    rej.append({"before": None, "after": c, "why": "new line outside Notes"})
            continue
        if op == "delete":
            for v in vs:
                if PLACEHOLDER.match(v) and section_is_notes(V, V.index(v)):
                    continue      # the empty "- " under Notes was consumed by a real note line
                if freeform:
                    acc.append({"kind": "delete", "before": v, "after": None})
                else:
                    rej.append({"before": v, "after": None, "why": "line missing on the calendar copy"})
            continue
        # replace with unequal counts: a Notes placeholder growing into lines, else pairwise, the rest rejected
        placeholders = [v for v in vs if PLACEHOLDER.match(v) and section_is_notes(V, V.index(v))]
        if placeholders and all(PLACEHOLDER.match(v) for v in vs) and all(section_is_notes(C, C.index(c)) for c in cs):
            first = True
            for c in cs:
                if c in vset:
                    continue
                acc.append({"kind": "note", "before": placeholders[0] if first else None, "after": c,
                            "anchor": None if first else cs[cs.index(c) - 1]})
                first = False
            continue
        for k in range(max(len(vs), len(cs))):
            v = vs[k] if k < len(vs) else None; c = cs[k] if k < len(cs) else None
            if v is not None and c is not None:
                kind, why = classify(v, c, freeform)
                if kind in ("tick", "number", "tick+number", "edit"):
                    acc.append({"kind": kind, "before": v, "after": c}); continue
            if c is not None and c in vset:
                continue
            if c is not None and v is None and (freeform or section_is_notes(C, C.index(c))):
                acc.append({"kind": "insert" if freeform else "note", "before": None, "after": c,
                            "anchor": (cs[k - 1] if k > 0 else (V[i1 - 1] if i1 > 0 else None))}); continue
            if v is not None and c is None and freeform:
                acc.append({"kind": "delete", "before": v, "after": None}); continue
            rej.append({"before": v, "after": c, "why": "block edit (not a tick, number or note)"})
    return acc, rej


def apply_to_file(path: Path, accepted: list) -> tuple[int, list]:
    """Apply accepted changes to the current file text by exact line match. Returns (applied, dropped)."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    applied, dropped = 0, []
    for ch in accepted:
        if ch["kind"] == "delete":
            try:
                i = next(i for i, ln in enumerate(lines) if ln.rstrip() == ch["before"])
            except StopIteration:
                dropped.append(ch); continue
            del lines[i]; applied += 1
        elif ch.get("before") is not None and ch["kind"] in ("tick", "number", "tick+number", "note", "edit"):
            try:
                i = next(i for i, ln in enumerate(lines) if ln.rstrip() == ch["before"])
            except StopIteration:
                dropped.append(ch); continue
            lines[i] = ch["after"]; applied += 1
        elif ch["kind"] == "insert":
            idx = None
            if ch.get("anchor") is not None:
                idx = next((i for i, ln in enumerate(lines) if ln.rstrip() == ch["anchor"]), None)
            if idx is None:
                lines.append(ch["after"])
            else:
                lines.insert(idx + 1, ch["after"])
            applied += 1
        elif ch["kind"] == "note":
            # insert after the anchor line if present, else at the end of the Notes section
            idx = None
            if ch.get("anchor") is not None:
                idx = next((i for i, ln in enumerate(lines) if ln.rstrip() == ch["anchor"]), None)
            if idx is None:
                heads = [i for i, ln in enumerate(lines) if NOTES_HEAD.match(ln)]
                if not heads:
                    dropped.append(ch); continue
                idx = heads[-1]
                while idx + 1 < len(lines) and not HEAD.match(lines[idx + 1]) and lines[idx + 1].strip() != "---":
                    idx += 1
            lines.insert(idx + 1, ch["after"]); applied += 1
    if applied:
        path.write_text("\n".join(lines), encoding="utf-8")
    return applied, dropped


def journal(path: Path, lines: list):
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Down-pipe log\n\n*Calendar -> vault, ticks only. One line per decision; written by tools/down_pipe.py. Never hand-edit.*\n\n")
        f.write("\n".join(lines) + "\n")


def run_pass(cfg: dict, svc=None, *, mode: str | None = None, dry_run: bool = False, only_rels: set | None = None) -> int:
    """One down-pipe pass over every published note in scope (or only_rels). Returns lines applied."""
    class A:  # the CLI namespace the body below reads
        pass
    a = A(); a.dry_run = dry_run
    mode = mode or cfg.get("down_pipe") or "off"
    if mode == "off":
        if only_rels is None:
            print("down-pipe is off for this instance (down_pipe = \"log\" or \"apply\" in comms.toml); nothing done")
        return 0
    if not cfg.get("publish_dir"):
        print("down-pipe needs the publisher (publish_dir); nothing done"); return 0
    vault = Path(cfg["publish_dir"]).expanduser().resolve()
    state = pub.load_state(Path(cfg.get("publish_state") or (CC / "state" / "publish.json")))
    folders, notes = state["folders"], state["notes"]
    # Folder MODES (2026-09-24, publish_layers `path=year|label|mode`) are the scope when any folder carries one:
    # interactive and freeform folders are read, readonly ones never are. Without modes the older fences apply
    # (down_pipe_layers / down_pipe_folders / down_pipe_apply_folders / down_pipe_freeform_folders).
    by_mode = any(f.get("mode") for f in folders.values())
    layers = {str(y) for y in (cfg.get("down_pipe_layers") or [])}
    if not layers and not by_mode:
        print("down_pipe_layers is empty and no folder carries a mode: nothing is in scope; nothing done"); return 0
    max_lines = int(cfg.get("down_pipe_max_lines") or 50)
    jpath = Path(cfg.get("down_pipe_journal") or (vault / "The Warehouse/Records/Down-pipe log.md")).expanduser()
    snap_cmd = cfg.get("down_pipe_snapshot_cmd")
    if by_mode:
        allowed = {fk: f for fk, f in folders.items() if f.get("mode") in ("interactive", "freeform") and f.get("path")}
    else:
        allowed = {fk: f for fk, f in folders.items() if str(f.get("layer")) in layers and f.get("path")}
    only = {x.strip("/") for x in (cfg.get("down_pipe_folders") or [])}      # optional narrower fence: folder paths or keys
    if only and not by_mode:
        allowed = {fk: f for fk, f in allowed.items() if fk in only or f.get("path") in only or f.get("spec") in only}
        if not allowed:
            print("down_pipe_folders names no mapped folder in the allowed layers; nothing in scope"); return 0
    roots = [(vault / f["path"]).resolve() for f in allowed.values()]
    key2name = {n["key"]: Path(rel).stem for rel, n in notes.items() if n.get("key")}
    name2key = {stem: key for key, stem in key2name.items()}
    apply_only = {x.strip("/") for x in (cfg.get("down_pipe_apply_folders") or [])}
    freeform_set = {x.strip("/") for x in (cfg.get("down_pipe_freeform_folders") or [])}
    def is_freeform(fk):
        f = folders[fk]
        if by_mode:
            return f.get("mode") == "freeform"
        return bool(freeform_set) and (fk in freeform_set or f.get("path") in freeform_set or f.get("spec") in freeform_set)
    def folder_mode(fk):
        if mode != "apply":
            return mode
        if by_mode:
            return "apply"        # readonly folders are outside `allowed`; every folder in scope writes
        if apply_only and not (fk in apply_only or folders[fk].get("path") in apply_only or folders[fk].get("spec") in apply_only):
            return "log"
        return "apply"
    svc = svc or cp.get_service(cfg); cal = cfg["calendar_id"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    jl, total_acc, total_rej, applied_total, snap = [], 0, 0, 0, None

    # ---- calendar-born notes in PUBLISHER folders (2026-09-22, operator's ACH workflow) -------------------
    # A "Note: <title>" event on a down-pipe layer day that no writer owns, whose location names one of the
    # allowed publisher folders (p<folder key>, or the bare folder key), becomes a vault file in that folder
    # and the SAME event is adopted as the note's calendar copy (no duplicate). A trailing "/" on the title is
    # the mirror's folder marker; here a folder cannot be created (publisher folders are mapped in config), so
    # it is stripped and reported. If the file already exists, the vault body wins and the event is adopted.
    born = 0
    tag = "[dry] " if a.dry_run else ""
    if mode == "apply" and not a.dry_run:
        state_path = Path(cfg.get("publish_state") or (CC / "state" / "publish.json"))
        pubevs = pub.list_events(svc, cal, cfg)
        # the phone's default day is today, so "Note:" events on recent real dates are candidates too
        from datetime import date as _d, timedelta as _td
        cp.pace(); recent = svc.events().list(calendarId=cal, timeMin=f"{_d.today() - _td(days=7)}T00:00:00Z",
                                              timeMax=f"{_d.today() + _td(days=2)}T00:00:00Z", singleEvents=True, maxResults=250).execute().get("items", [])
        seen_ids = {e["id"] for e in pubevs}
        pubevs = pubevs + [e for e in recent if e["id"] not in seen_ids]
        taken = {t for e in pubevs for t in (e.get("location") or "").split() if links.is_key(t)}
        taken |= {v["key"] for v in notes.values() if v.get("key")} | set(folders)
        for ev in pubevs:
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            title = (ev.get("summary") or "").strip()
            if priv.get("comms_writer") or priv.get("comms_state") or not re.match(r"^Note:\s*\S", title):
                continue
            toks = (ev.get("location") or "").split()
            fk = next((t[1:] for t in toks if t.startswith("p") and t[1:] in allowed), None) \
                 or next((t for t in toks if t in allowed), None)
            if not fk or folder_mode(fk) != "apply":
                continue
            was_folder = title.endswith("/")
            stem = re.sub(r"^Note:\s*", "", title).rstrip("/").strip()
            stem = re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", stem)
            rel = f"{folders[fk]['path']}/{stem}.md"; target = vault / rel
            if rel in notes:
                # the note already exists and has its own calendar copy: this event is a duplicate author copy
                if ev["id"] not in (notes[rel].get("event_ids") or []):
                    cp.write_event(svc, cal, ev["id"], {"summary": f"✓ {title.rstrip('/')} — landed in the vault; the published copy is on the {folders[fk]['layer']} layer, this one can be deleted",
                                                         "extendedProperties": {"private": {"comms_kind": "reply", "comms_state": "done", "comms_writer": "inbox"}}}, existing=ev, verify=False)
                    print(f"{tag}born    {rel}: already published; the author's copy {ev['id']} marked as landed")
                    jl.append(f"- {stamp} · apply · BORN-DUP · `{rel}` · author event {ev['id']} marked landed (published copy exists)")
                continue
            body = links.down(links.strip_key_line(normalise_html(ev.get("description") or "")), key2name).rstrip() + "\n"
            if target.exists():
                print(f"{tag}born    {rel}: file already exists, vault body kept; event adopted as its calendar copy")
            else:
                if snap is None and snap_cmd:
                    r = subprocess.run(snap_cmd, shell=True, capture_output=True, text=True)
                    snap = (r.stdout.strip().splitlines() or ["snapshot"])[-1]
                    jl.append(f"- {stamp} · apply · SNAPSHOT before writes · {snap}")
                target.parent.mkdir(parents=True, exist_ok=True); target.write_text(body, encoding="utf-8")
                print(f"{tag}born    {rel}: created from the calendar ({len(body)} chars)")
            key = links.mint(taken)
            notes[rel] = {"key": key, "event_ids": [ev["id"]], "hash": None, "folder": fk, "inode": None, "title": None}
            newpriv = {"comms_kind": "note", "comms_writer": pub.WRITER, "publish_path": rel, "publish_key": key, "publish_folder": fk}
            # the adopted event moves onto the folder's layer day (all-day), where the publisher lists and owns it;
            # left on the phone's real date it would sit outside the publisher's window and be re-inserted
            from datetime import date as _dd, timedelta as _tdd
            day = f"{folders[fk]['layer']}-01-01"; nxt = (_dd.fromisoformat(day) + _tdd(days=1)).isoformat()
            try:
              cp.write_event(svc, cal, ev["id"], {"summary": f"Note: {stem}", "location": f"{key} p{fk}",
                                                 "start": {"date": day, "dateTime": None, "timeZone": None},
                                                 "end": {"date": nxt, "dateTime": None, "timeZone": None}, "reminders": {"useDefault": False},
                                                 "transparency": "transparent",
                                                 "extendedProperties": {"private": newpriv}}, existing=ev, verify=False)
            except Exception as ex:  # noqa: BLE001
                print(f"born    {rel}: could not adopt event {ev['id']} ({str(ex)[:120]}); the publisher will give the note its own copy")
                notes[rel]["event_ids"] = []
            jl.append(f"- {stamp} · apply · BORN · `{rel}` · from calendar event {ev['id']} (key {key}, parent {fk})"
                      + (" · trailing slash ignored: a publisher folder cannot make sub-folders from the calendar" if was_folder else ""))
            if was_folder:
                print(f"{tag}born    {rel}: the title's trailing slash was ignored (folders are not made this way here)")
            born += 1
        if born:
            state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False))
            print(f"calendar-born: {born} note(s) registered; the publisher renders them on its next pass")

    for rel, n in notes.items():
        if n.get("folder") not in allowed:
            continue
        if only_rels is not None and rel not in only_rels:
            continue
        p = (vault / rel)
        try:
            rp = p.resolve(strict=True)
        except OSError:
            continue
        if p.is_symlink() or not any(str(rp).startswith(str(r) + os.sep) for r in roots):
            print(f"{tag}REFUSED outside scope: {rel}"); continue
        fmode = folder_mode(n["folder"])
        write = fmode == "apply" and not a.dry_run
        tag = {"log": "[log] ", "apply": "[dry] " if a.dry_run else ""}[fmode]
        cal_text = read_calendar_copy(svc, cal, n, key2name)
        if cal_text is None:
            continue
        vault_text = pub.canonical(rp.read_text(encoding="utf-8", errors="replace"))
        # Compare against the vault RENDERED the way the publisher renders it (links glued, parts split at the cap,
        # then translated back down), so part-boundary artefacts cancel out and only a real edit shows.
        rendered = links.down("\n\n".join(pub.split_parts(links.up(vault_text, name2key), pub.BODY_CAP)), key2name)
        if pub.h(cal_text.strip()) in (n.get("hash"), pub.h(rendered.strip())):
            continue      # the calendar copy is exactly what was published (or what would be now): no phone edit
        free = is_freeform(n["folder"])
        stale = bool(free and n.get("hash") and pub.h(vault_text.strip()) != n["hash"])
        acc, rej = diff_note(rendered, cal_text, free)
        if stale:
            # The vault moved since this note was last published, so the calendar copy is stale: applying its
            # edits or deletes would read the vault's new lines as reverts. Pure INSERTS and ticks revert
            # nothing, so they still land (2026-09-24: a Must-order row typed on the phone right after a
            # regeneration must not wait a cycle and then be overwritten); everything else waits for the
            # publisher to re-render — the vault always wins on content.
            safe = [c for c in acc if c["kind"] in ("insert", "note", "tick")]
            dropped = len(acc) - len(safe)
            acc = safe
            print(f"{tag}stale   {rel}: vault changed since the last publish; {len(safe)} insert/tick line(s) kept, "
                  f"{dropped} edit/delete line(s) skipped until the calendar copy is re-rendered")
            if dropped:
                jl.append(f"- {stamp} · {fmode} · STALE · `{rel}` · the vault changed since this note was published: "
                          f"{dropped} edited/deleted line(s) not applied (the vault wins), {len(safe)} inserted line(s) applied")
            if not acc:
                continue
        if not acc and not rej:
            continue
        for r in rej:
            total_rej += 1
            print(f"{tag}reject  {rel}: {r['why']}: {(r['after'] or r['before'] or '')[:100]}")
            jl.append(f"- {stamp} · {fmode} · REJECT · `{rel}` · {r['why']} · `{(r['after'] or r['before'] or '')[:160]}`")
        if not acc:
            continue
        if total_acc + len(acc) > max_lines:
            print(f"{tag}CAP {max_lines} lines per pass reached; the rest waits for the next pass"); break
        total_acc += len(acc)
        for c in acc:
            print(f"{tag}accept  {rel}: {c['kind']}: {(c['after'] or c['before'] or '')[:100]}")
        if write:
            if snap is None and snap_cmd:
                r = subprocess.run(snap_cmd, shell=True, capture_output=True, text=True)
                snap = (r.stdout.strip().splitlines() or ["snapshot"])[-1]
                jl.append(f"- {stamp} · apply · SNAPSHOT before writes · {snap}")
            applied, dropped = apply_to_file(rp, acc)
            applied_total += applied
            for c in acc:
                if c in dropped:
                    jl.append(f"- {stamp} · apply · DROPPED (line no longer in the vault) · `{rel}` · `{(c['before'] or c['after'])[:160]}`")
                else:
                    jl.append(f"- {stamp} · apply · {c['kind'].upper()} · `{rel}` · before `{(c['before'] or '')[:120]}` · after `{(c['after'] or '')[:120]}`")
            print(f"applied {applied} line(s) to {rel}" + (f", {len(dropped)} dropped" if dropped else ""))
        else:
            for c in acc:
                jl.append(f"- {stamp} · {fmode}{' dry' if a.dry_run else ''} · WOULD {c['kind'].upper()} · `{rel}` · `{(c['after'] or c['before'] or '')[:160]}`")
    if not a.dry_run:
        journal(jpath, jl)
    if only_rels is not None and not total_acc and not total_rej:
        return 0
    if by_mode:
        scope = "; ".join(f"{m}: " + ", ".join(sorted(f["path"] for f in folders.values() if f.get("mode") == m and f.get("path")))
                          for m in ("interactive", "freeform") if any(f.get("mode") == m for f in folders.values()))
        head = f"down-pipe pass ({mode} by folder mode — {scope})"
    else:
        head = (f"down-pipe pass ({mode}{', apply only ' + ', '.join(sorted(apply_only)) if apply_only else ''}"
                f"{'; free-form: ' + ', '.join(sorted(freeform_set)) if freeform_set else ''})")
    print(f"{head}: {total_acc} accepted, {total_rej} rejected, {applied_total} applied" + (f", snapshot {snap}" if snap else ""))
    return applied_total


def on_event(svc, cfg: dict, ev: dict) -> int:
    """Poller hook (2026-09-19, operator go): a changed event carrying publish_path is diffed and applied on the spot,
    then that note is re-rendered by a lock-respecting publish pass (skipped if the timer pass holds the lock:
    the calendar copy already carries the tick, so the next timer pass finds nothing to do). Never raises."""
    try:
        if (cfg.get("down_pipe") or "off") == "off":
            return 0
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        rel = priv.get("publish_path")
        if not rel or priv.get("comms_writer") != pub.WRITER:
            return 0
        applied = run_pass(cfg, svc, only_rels={rel})
        if applied:
            lock = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "comms-publish.lock"
            py = str(CC / ".venv" / "bin" / "python")
            r = subprocess.run(["flock", "-n", str(lock), py, str(CC / "tools" / "publish_up.py"), "--apply"],
                               capture_output=True, text=True, timeout=300)
            print("down-pipe: re-rendered after apply" if r.returncode == 0 else
                  "down-pipe: re-render deferred to the timer pass (lock held)" if r.returncode == 1 else
                  f"down-pipe: re-render failed rc={r.returncode}: {(r.stderr or r.stdout)[-300:]}")
        return applied
    except Exception as e:  # noqa: BLE001
        print(f"down-pipe hook error (poller continues): {e}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["off", "log", "apply"])
    ap.add_argument("--dry-run", action="store_true", help="never write, whatever the mode")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    return 3 if run_pass(cfg, mode=a.mode, dry_run=a.dry_run) else 0


if __name__ == "__main__":
    sys.exit(main())
