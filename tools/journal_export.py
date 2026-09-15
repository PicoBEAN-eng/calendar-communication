#!/usr/bin/env python3
"""Export this stream's calendar notes and finished threads into an Obsidian vault (the "calendar journal").

One-way, calendar → vault. Opt-in per instance: set `journal_dir = "/path/to/vault"` in comms.toml; without
it nothing is written. Runs from deploy/comms-journal.timer, idempotent.

What goes across
  Note: events (title grammar or anchor date)        → <vault>/Notes/<title>.md          (anchor-date notes)
                                                       <vault>/Notes/<date> <title>.md   (dated notes)
  finished threads (✓ done, thread transcript exists) → <vault>/Threads/<date> <title>.md

What is stripped (the translation layer)
  state prefixes (⏳ ✓ ?), progress heartbeats, gear tags in title "(model effort)" and description
  "[model effort]", every private extended property, event ids, etags, claim/reply timestamps.
What is kept
  title, description as markdown, calendar date, the thread transcript from spool/threads rendered as a
  conversation, and a stable frontmatter `id` (sha1 of the event id, 12 hex) so retitling survives.

Files the operator has edited in Obsidian are never overwritten: a `sync` hash in the frontmatter records
what we last wrote; if the body no longer matches it, the file is left alone and reported.
"""
import argparse, hashlib, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC))
import comms_poller as cp  # noqa: E402

LOOKBACK_DAYS = 120
MACHINERY = {"ambient", "digest", "welcome", "closed", "outstanding"}
MACHINE_WRITERS = {"todo_sweep", "wakeup", "note_protocol", "meridian_day"}
TITLE_GEAR = re.compile(r"\s*\(([^()]{0,40})\)\s*$")
THREAD_HEAD = re.compile(r"^# Thread \S+ — (.*?)(?:\s*\[[^\]]{0,40}\])?\s*$")
TURN_HEAD = re.compile(r"^## Turn (\d+) — (\S+) (asked|replied(?: \([a-z]+\))?) · (\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})")
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def stable_id(event_id: str) -> str:
    return hashlib.sha1(event_id.encode()).hexdigest()[:12]


def clean_title(summary: str) -> str:
    t = cp.base_title(summary or "").strip()
    t = TITLE_GEAR.sub("", t).strip()
    return t or "untitled"


def clean_desc(desc: str) -> str:
    d = (desc or "").replace("\r\n", "\n")
    d = cp.DESC_GEAR.sub("", d, count=1)
    return d.strip()


def safe_name(s: str, limit: int = 80) -> str:
    s = UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].rstrip(" .") or "untitled"


def event_date(ev: dict, tz) -> date:
    start = ev.get("start") or {}
    if start.get("date"):
        return date.fromisoformat(start["date"])
    return datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(tz).date()


def yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(front: dict, body: str) -> str:
    body = body.strip() + "\n"
    front["sync"] = hashlib.sha1(body.encode()).hexdigest()[:12]
    fm = "\n".join(f"{k}: {yaml_str(v) if isinstance(v, str) else v}" for k, v in front.items())
    return f"---\n{fm}\n---\n\n{body}"


def split(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) of a file we wrote earlier; ({}, text) otherwise."""
    m = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(": ")
        fm[k] = v.strip('"')
    return fm, m.group(2)


def render_thread(transcript: str) -> str:
    out = []
    for line in transcript.splitlines():
        if THREAD_HEAD.match(line):
            continue
        m = TURN_HEAD.match(line)
        if m:
            n, who, what, d, hm = m.groups()
            what = "asked" if what == "asked" else f"replied ({what.split('(')[1][:-1]})" if "(" in what else "replied"
            out.append(f"### Turn {n} · {who} {what} · {d} {hm}")
            continue
        out.append(line)
    return "\n".join(out).strip()


def write(path: Path, content: str, dry: bool, report: list) -> None:
    new_fm, new_body = split(content)
    if path.exists():
        old_fm, old_body = split(path.read_text(encoding="utf-8"))
        if old_fm.get("sync") and hashlib.sha1(old_body.strip().encode() + b"\n").hexdigest()[:12] != old_fm["sync"]:
            report.append(f"edited locally, left alone: {path.name}")
            return
        if old_fm.get("sync") == new_fm.get("sync"):
            return
        verb = "update"
    else:
        verb = "create"
    report.append(f"{verb}: {path.relative_to(path.parents[1])}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def list_events(svc, cal, lo: str, hi: str) -> list[dict]:
    items, page = [], None
    while True:
        resp = svc.events().list(calendarId=cal, timeMin=lo, timeMax=hi, singleEvents=True, showDeleted=False,
                                 maxResults=2500, pageToken=page).execute()
        items += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    vault = cfg.get("journal_dir")
    if not vault:
        print("journal export is off for this instance (set journal_dir in comms.toml); nothing written")
        return
    vault = Path(vault).expanduser()
    if not vault.is_dir():
        sys.exit(f"journal_dir {vault} is not a directory")
    tz = ZoneInfo(cfg["timezone"])
    stream = cfg["stream"]
    today = datetime.now(tz).date()
    svc = cp.get_service(cfg)
    cal = cfg["calendar_id"]
    anchor = date.fromisoformat(cfg["note_anchor_date"])

    events = list_events(svc, cal, f"{anchor.isoformat()}T00:00:00Z", f"{(anchor + timedelta(days=1)).isoformat()}T00:00:00Z")
    lo, hi = today - timedelta(days=a.days), today + timedelta(days=2)
    events += list_events(svc, cal, f"{lo.isoformat()}T00:00:00Z", f"{hi.isoformat()}T00:00:00Z")
    # finished traffic is archived into the band, `traffic_offset_years` ahead, original date stamped
    band = int(cfg.get("traffic_offset_years") or 0)
    if band:
        events += list_events(svc, cal, f"{lo.replace(year=lo.year + band).isoformat()}T00:00:00Z",
                              f"{hi.replace(year=hi.year + band).isoformat()}T00:00:00Z")
    seen, report = set(), []
    for ev in events:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        priv = (ev.get("extendedProperties") or {}).get("private", {})
        if priv.get("comms_writer") in MACHINE_WRITERS or priv.get("comms_kind") in MACHINERY:
            continue  # ambient spans, digests, welcome, index, instructions: calendar furniture, not journal
        when = event_date(ev, tz)
        if priv.get("comms_archived_from"):
            when = date.fromisoformat(priv["comms_archived_from"][:10])
        elif band and when.year - today.year >= band - 1:
            when = when.replace(year=when.year - band)  # created straight into the band
        title = clean_title(ev.get("summary"))
        desc = clean_desc(ev.get("description"))
        base = {"id": stable_id(ev["id"]), "stream": stream, "date": when.isoformat(), "title": title}
        if cp.is_note(ev, cfg):
            t = re.sub(r"^\s*note\s*[:\-]?\s*", "", title, flags=re.I).strip() or title
            base["title"] = t
            base["kind"] = "note"
            name = safe_name(t) if when == anchor else f"{when.isoformat()} {safe_name(t)}"
            body = f"# {t}\n\n{desc}" if desc else f"# {t}"
            write(vault / "Notes" / f"{name}.md", render(base, body), a.dry_run, report)
        elif cp.comms_state(ev) == "done" or (ev.get("summary") or "").startswith(cp.PREFIX.get("done", "✓ ")):
            tp = cp.thread_path(cfg, ev["id"])
            if not tp.exists():
                continue  # a follow-up turn; its transcript lives with the root event
            base["kind"] = "thread"
            body = f"# {title}\n\n{render_thread(tp.read_text(encoding='utf-8'))}"
            write(vault / "Threads" / f"{when.isoformat()} {safe_name(title)}.md", render(base, body), a.dry_run, report)
    for line in report:
        print(line)
    print(f"journal export: {len(report)} change(s){' (dry run)' if a.dry_run else ''}")


if __name__ == "__main__":
    main()
