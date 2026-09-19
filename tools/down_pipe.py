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
    down_pipe_layers = ["3060"]
    down_pipe_folders = ["! Active/Location Checks"]   # optional: narrow the whole scope to these folder paths or keys
    down_pipe_apply_folders = ["! Active/Location Checks"]   # optional: with "apply", only these folders write; the rest of the scope logs
    down_pipe_max_lines = 50
    down_pipe_journal = "/path/to/vault/The Warehouse/Records/Down-pipe log.md"
    down_pipe_snapshot_cmd = "/home/x/woolly-workplace/bin/vault-snapshot"

  down_pipe.py [--mode off|log|apply] [--dry-run] [--config comms.toml]      exit 0; 3 = something was applied
"""
import argparse, difflib, json, os, re, subprocess, sys
from datetime import datetime
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402
import links  # noqa: E402
import publish_up as pub  # noqa: E402

TICK_OPEN = re.compile(r"^(\s*[-*]\s+)\[ \](.*)$")
TICK_DONE = re.compile(r"^(\s*[-*]\s+)\[[xX]\](.*)$")
BLANK = re.compile(r"_{2,}|(?<![\w_])_(?![\w_])")
NUMBER = re.compile(r"\d+")
NOTES_HEAD = re.compile(r"^#{1,6}\s*(?:\d+\s*·\s*)?Notes\b", re.I)
HEAD = re.compile(r"^#{1,6}\s")
PLACEHOLDER = re.compile(r"^\s*[-*]\s*$")


def classify(v: str, c: str):
    """(kind, None) for an accepted line pair, else (None, reason)."""
    if v == c:
        return "same", None
    mo, mc = TICK_OPEN.match(v), TICK_DONE.match(c)
    if mo and mc and mo.group(1) == mc.group(1):
        if mo.group(2) == mc.group(2):
            return "tick", None
        # tick AND a number on the same line
        if BLANK.search(mo.group(2)) and blank_to_number(mo.group(2), mc.group(2)):
            return "tick+number", None
        return None, "ticked line also changed its text"
    if BLANK.search(v) and blank_to_number(v, c):
        return "number", None
    return None, "text changed (not a tick, a number over a blank, or a note)"


def blank_to_number(v: str, c: str) -> bool:
    """True iff c equals v with every blank replaced by digits (at least one blank filled)."""
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
        body = (ev.get("description") or "").rstrip()
        if links.key_of(body):
            body = body[: body.rfind("\n")].rstrip() if "\n" in body else ""
        parts.append(body)
    if not parts:
        return None
    return links.down("\n\n".join(parts), key2name)


def diff_note(vault_text: str, cal_text: str):
    """Returns (accepted: [dict], rejected: [dict]). Blank lines are ignored on both sides;
    a calendar line that duplicates an existing vault line (repeated table headers) is ignored."""
    V = [ln.rstrip() for ln in vault_text.splitlines() if ln.strip()]
    C = [ln.rstrip() for ln in cal_text.splitlines() if ln.strip()]
    vset = set(V)
    acc, rej = [], []
    sm = difflib.SequenceMatcher(a=V, b=C, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        vs, cs = V[i1:i2], C[j1:j2]
        if op == "replace" and len(vs) == len(cs):
            for v, c in zip(vs, cs):
                kind, why = classify(v, c)
                if kind in ("tick", "number", "tick+number"):
                    acc.append({"kind": kind, "before": v, "after": c})
                elif PLACEHOLDER.match(v) and section_is_notes(V, V.index(v)) and c.strip():
                    acc.append({"kind": "note", "before": v, "after": c})
                elif kind is None:
                    rej.append({"before": v, "after": c, "why": why})
            continue
        if op == "insert":
            for c in cs:
                if c in vset:
                    continue      # repeated table header from the part split, or an echo of an existing line
                if section_is_notes(C, C.index(c)):
                    anchor = V[i1 - 1] if i1 > 0 else None
                    acc.append({"kind": "note", "before": None, "after": c, "anchor": anchor})
                else:
                    rej.append({"before": None, "after": c, "why": "new line outside Notes"})
            continue
        if op == "delete":
            for v in vs:
                if PLACEHOLDER.match(v) and section_is_notes(V, V.index(v)):
                    continue      # the empty "- " under Notes was consumed by a real note line
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
                kind, why = classify(v, c)
                if kind in ("tick", "number", "tick+number"):
                    acc.append({"kind": kind, "before": v, "after": c}); continue
            if c is not None and c in vset:
                continue
            if c is not None and v is None and section_is_notes(C, C.index(c)):
                acc.append({"kind": "note", "before": None, "after": c, "anchor": cs[k - 1] if k > 0 else None}); continue
            rej.append({"before": v, "after": c, "why": "block edit (not a tick, number or note)"})
    return acc, rej


def apply_to_file(path: Path, accepted: list) -> tuple[int, list]:
    """Apply accepted changes to the current file text by exact line match. Returns (applied, dropped)."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    applied, dropped = 0, []
    for ch in accepted:
        if ch["kind"] in ("tick", "number", "tick+number", "note") and ch.get("before") is not None:
            try:
                i = next(i for i, ln in enumerate(lines) if ln.rstrip() == ch["before"])
            except StopIteration:
                dropped.append(ch); continue
            lines[i] = ch["after"]; applied += 1
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["off", "log", "apply"])
    ap.add_argument("--dry-run", action="store_true", help="never write, whatever the mode")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    mode = a.mode or cfg.get("down_pipe") or "off"
    if mode == "off":
        print("down-pipe is off for this instance (down_pipe = \"log\" or \"apply\" in comms.toml); nothing done"); return 0
    if not cfg.get("publish_dir"):
        print("down-pipe needs the publisher (publish_dir); nothing done"); return 0
    vault = Path(cfg["publish_dir"]).expanduser().resolve()
    layers = {str(y) for y in (cfg.get("down_pipe_layers") or [])}
    if not layers:
        print("down_pipe_layers is empty: nothing is in scope; nothing done"); return 0
    max_lines = int(cfg.get("down_pipe_max_lines") or 50)
    jpath = Path(cfg.get("down_pipe_journal") or (vault / "The Warehouse/Records/Down-pipe log.md")).expanduser()
    snap_cmd = cfg.get("down_pipe_snapshot_cmd")
    state = pub.load_state(Path(cfg.get("publish_state") or (CC / "state" / "publish.json")))
    folders, notes = state["folders"], state["notes"]
    allowed = {fk: f for fk, f in folders.items() if str(f.get("layer")) in layers and f.get("path")}
    only = {x.strip("/") for x in (cfg.get("down_pipe_folders") or [])}      # optional narrower fence: folder paths or keys
    if only:
        allowed = {fk: f for fk, f in allowed.items() if fk in only or f.get("path") in only or f.get("spec") in only}
        if not allowed:
            print("down_pipe_folders names no mapped folder in the allowed layers; nothing in scope"); return 0
    roots = [(vault / f["path"]).resolve() for f in allowed.values()]
    key2name = {n["key"]: Path(rel).stem for rel, n in notes.items() if n.get("key")}
    apply_only = {x.strip("/") for x in (cfg.get("down_pipe_apply_folders") or [])}
    def folder_mode(fk):
        if mode != "apply":
            return mode
        if apply_only and not (fk in apply_only or folders[fk].get("path") in apply_only or folders[fk].get("spec") in apply_only):
            return "log"
        return "apply"
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    jl, total_acc, total_rej, applied_total, snap = [], 0, 0, 0, None
    for rel, n in notes.items():
        if n.get("folder") not in allowed:
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
        acc, rej = diff_note(vault_text, cal_text)
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
            print(f"{tag}accept  {rel}: {c['kind']}: {c['after'][:100]}")
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
                    jl.append(f"- {stamp} · apply · {c['kind'].upper()} · `{rel}` · before `{(c['before'] or '')[:120]}` · after `{c['after'][:120]}`")
            print(f"applied {applied} line(s) to {rel}" + (f", {len(dropped)} dropped" if dropped else ""))
        else:
            for c in acc:
                jl.append(f"- {stamp} · {fmode}{' dry' if a.dry_run else ''} · WOULD {c['kind'].upper()} · `{rel}` · `{c['after'][:160]}`")
    if not a.dry_run:
        journal(jpath, jl)
    print(f"down-pipe pass ({mode}{', apply only ' + ', '.join(sorted(apply_only)) if apply_only else ''}): {total_acc} accepted, {total_rej} rejected, {applied_total} applied"
          + (f", snapshot {snap}" if snap else ""))
    return 3 if applied_total else 0


if __name__ == "__main__":
    sys.exit(main())
