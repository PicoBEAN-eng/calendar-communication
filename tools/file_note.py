#!/usr/bin/env python3
"""File a note into the vault so the publisher carries it to the calendar: the ONE way an agent lays a
context note (operator 2026-10-01: "nothing exists only on the calendar" — every note findable in both,
same folder structure, keyed and indexed like published notes).

The vault file is the note. It goes into a folder that publish_layers maps, the publisher mints it (key in
the vault frontmatter, key line + parent token on the calendar, listed in its folder's index on the
front-door day two), and later edits flow the usual way. Never insert a "Note:" event by hand any more.

  file_note.py --title "Audit 2026-10-01" --body-file /tmp/x.md [--folder "<vault-relative folder>"]
               [--replace] [--apply]
      Writes <publish_dir>/<folder>/<title>.md (folder defaults to `note_home` in comms.toml), then starts a
      publish pass (the service; never inline — a pass is ~30 minutes). Refuses a folder no publish layer covers (the note would
      exist only in the vault) and an existing file unless --replace. A leading "Note: " on the title is
      dropped. Body from --body-file or stdin. Dry run without --apply.

  file_note.py --backfill [--route "Title=folder" ...] [--apply]
      Every calendar-only note in the band (comms_kind note, written by anyone but the publisher or the
      mirror, no vault path; this stream's generated relay instructions and Note: Index excepted, they are
      rebuilt from the repo) is written to the vault — into the --route folder for its title, else
      `note_home` — key line stripped, body otherwise verbatim. The publish pass then ADOPTS each event in
      place by title (publish_up.py, 2026-10-01): re-dated to the folder's layer, keyed, no duplicate.
"""
import argparse, re, subprocess, sys
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402
import links  # noqa: E402
import publish_up as pu  # noqa: E402
import note_protocol as npr  # noqa: E402

UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')   # mirror.file_name's rule: Obsidian-unsafe characters


def file_stem(title: str) -> str:
    return UNSAFE.sub("-", re.sub(r"^Note:\s*", "", title)).strip()


def covered(cfg, vault: Path, folder: str) -> str | None:
    """The publish layer spec that covers this vault-relative folder, or None."""
    folder = folder.strip("/")
    excludes = [x.strip("/") for x in (cfg.get("publish_exclude") or [])]
    if any(folder == x or folder.startswith(x + "/") for x in excludes):
        return None
    best = None
    for spec, *_ in pu.parse_layers(cfg):
        if any(ch in spec for ch in "*?[") or spec.startswith("k:"):
            continue
        spec = spec.strip("/")
        if folder == spec or folder.startswith(spec + "/"):
            best = spec if best is None or len(spec) > len(best) else best
    return best


def publish_pass(apply: bool) -> int:
    """Hand the note to the publisher. A full pass is ~30 minutes of paced API calls (the Warehouse tree), so
    this never runs one inline: it starts the publish service now (no wait; its own flock skips the start if a
    pass is already running, and the timer's next pass then carries the note), else leaves it to the timer."""
    if not apply:
        print("[dry] would start a publish pass (comms-publish.service)")
        return 0
    r = subprocess.run(["systemctl", "--user", "start", "--no-block", "comms-publish.service"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("publish pass started (comms-publish.service): the note reaches the calendar when it finishes, "
              "or on the next timer pass if one was already running")
    else:
        print("could not start comms-publish.service — the publish timer will carry the note: "
              + (r.stderr or "").strip()[-200:])
    return 0


def write_note(vault: Path, folder: str, title: str, body: str, replace: bool, apply: bool) -> Path | None:
    path = vault / folder / f"{file_stem(title)}.md"
    if path.exists() and not replace:
        print(f"exists, left alone: {path.relative_to(vault)}")
        return path
    print(f"{'' if apply else '[dry] '}write {path.relative_to(vault)} ({len(body)} chars)")
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return path


def calendar_only(svc, cfg) -> list:
    skip = {npr.protocol_title(cfg["stream"]), npr.INDEX_TITLE}
    out = []
    for e in pu.list_events(svc, cfg["calendar_id"], cfg):
        pv = (e.get("extendedProperties") or {}).get("private") or {}
        title = e.get("summary", "")
        if (pv.get("comms_kind") == "note" and pv.get("comms_writer") not in (pu.WRITER, "mirror")
                and not pv.get("mirror_path") and not pv.get("publish_path") and title not in skip):
            out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title")
    ap.add_argument("--body-file")
    ap.add_argument("--folder", help="vault-relative folder (default: note_home in comms.toml)")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--route", action="append", default=[], help='backfill: "Title=vault/relative/folder"')
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--write-only", action="store_true",
                    help="write the vault files, skip the publish pass (preview it with publish_up.py, dry)")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    if not cfg.get("publish_dir") or not cfg.get("publish_layers"):
        sys.exit("the publisher is off on this instance (publish_dir / publish_layers): nowhere to file a note")
    vault = Path(cfg["publish_dir"]).expanduser()
    home = (cfg.get("note_home") or "").strip("/")

    def check(folder: str) -> str:
        if not folder:
            sys.exit("no folder: pass --folder or set note_home in comms.toml")
        spec = covered(cfg, vault, folder)
        if spec is None:
            sys.exit(f"{folder!r} is not under any publish layer — the note would live only in the vault; "
                     "map the folder in publish_layers first")
        return folder

    if a.backfill:
        routes = {}
        for r in a.route:
            t, _, f = r.partition("=")
            routes[re.sub(r"^Note:\s*", "", t.strip())] = f.strip().strip("/")
        svc = cp.get_service(cfg)
        evs = calendar_only(svc, cfg)
        if not evs:
            print("no calendar-only notes — every note has a vault home")
            return 0
        titles = {}
        for e in evs:
            titles.setdefault(e.get("summary", ""), []).append(e)
        for title, group in sorted(titles.items()):
            if len(group) > 1:
                print(f"SKIP {title}: {len(group)} events share the title — file it by hand")
                continue
            e = group[0]
            bare = re.sub(r"^Note:\s*", "", title)
            folder = check(routes.get(bare) or home)
            body = links.strip_key_line(e.get("description") or "").strip()
            print(f"{title}  ({e['start'].get('date')}, writer {((e.get('extendedProperties') or {}).get('private') or {}).get('comms_writer')})")
            write_note(vault, folder, title, body, replace=False, apply=a.apply)
        return 0 if a.write_only else publish_pass(a.apply)

    if not a.title:
        sys.exit("--title is required (or --backfill)")
    body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else sys.stdin.read()
    if not body.strip():
        sys.exit("empty body")
    folder = check((a.folder or home).strip("/"))
    write_note(vault, folder, a.title, body, a.replace, a.apply)
    return 0 if a.write_only else publish_pass(a.apply)


if __name__ == "__main__":
    sys.exit(main() or 0)
