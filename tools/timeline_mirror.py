#!/usr/bin/env python3
"""Timeline mirror: every calendar-native event lands in the vault (operator 2026-10-03, Q2: "all of it").

The two-way mirror (tools/mirror.py) carries the layer band, where a note's home is a folder. This tool
carries everything else, one way, calendar -> vault: the daily welcome, the Outstanding digest, Meridian
spans and other ambient markers, relay traffic (requests and their in-place replies, wherever the stream's
traffic band puts them), transcripts, and any other event that is not a mirrored note. The vault copy is
<mirror_dir>/<timeline_dir>/<YYYY>/<YYYY-MM-DD>/<title>.md, filed under the event's REAL date (a band-dated
event is decoded back by the band offset), so Obsidian reads "what happened on that day" as a folder.

Identity travels in frontmatter: the event id (the key for one-way copies; a minted five-character key is
added when these notes join the two-way mirror), the kind and writer, start and end as written on the
calendar (timed or all-day), the band date when the event sits in a band, the sender's email (the
calendar's own creator field: this is the sender identity Q3 asks for), and a hash of the body so unchanged
events cost nothing. Deletion on the calendar removes the file on the next pass. Events already carried by
the two-way mirror (private mirror_path) are skipped.

  timeline_mirror.py [--since DAYS] [--dry-run] [--config comms.toml]
    --since: how many real days back to read (default 14); the far bands are decoded, so a band event from
    three weeks ago is outside the window just like a live one. Rare full catch-up: --since 400.
"""
import argparse, hashlib, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402

UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
TIMELINE_DIR = "Calendar mirror/Timeline"
TRANSCRIPT_OFFSET = 6000          # transcripts sit at real date + 6000 (Frames convention, 8026+)
LEGACY_TRAFFIC_OFFSET = 2000      # traffic before the 2026-10-03 re-base (4026+); decoded the same way


def file_stem(title: str) -> str:
    return (UNSAFE.sub("-", re.sub(r"^[✓✔⏳?]\s*", "", title)).strip() or "event")[:90]


def when(ev) -> tuple[str, bool]:
    st = ev["start"]
    return (st["dateTime"], True) if "dateTime" in st else (st["date"], False)


def decode(day: date, offsets: list[int]) -> tuple[date, int]:
    """A date in a band maps back to its real day by the band's offset; 0 = a live date."""
    for off in sorted(offsets, reverse=True):
        if off and day.year >= 2020 + off:
            try:
                return day.replace(year=day.year - off), off
            except ValueError:            # 29 Feb decoded into a non-leap year
                return day.replace(year=day.year - off, day=28), off
    return day, 0


def body_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def frontmatter(ev, real_day: date, off: int, start: str, end: str, timed: bool, h: str) -> str:
    priv = ev.get("extendedProperties", {}).get("private", {})
    rows = [("event", ev["id"]), ("title", ev.get("summary", "").replace('"', "'")),
            ("day", real_day.isoformat()), ("start", start), ("end", end), ("timed", "true" if timed else "false")]
    if off:
        rows.append(("band_offset_years", str(off)))
    kind = priv.get("comms_kind") or ("traffic" if priv.get("comms_state") else "event")
    rows += [("kind", kind)]
    for k in ("comms_writer", "comms_state", "comms_turn", "comms_stream", "comms_from", "comms_to", "comms_a2a"):
        if priv.get(k):
            rows.append((k.replace("comms_", ""), priv[k]))
    sender = (ev.get("creator") or {}).get("email")
    if sender:
        rows.append(("sender", sender))
    rows.append(("event_hash", h))
    return "---\n" + "\n".join(f'{k}: "{v}"' if k in ("title", "sender") else f"{k}: {v}" for k, v in rows) + "\n---\n"


def list_window(svc, cal, lo: date, hi: date, offsets: list[int]):
    """Events on the live window and on the same window inside every band."""
    out = []
    for off in [0] + [o for o in offsets if o]:
        a, b = lo.replace(year=lo.year + off), hi.replace(year=hi.year + off)
        page = None
        while True:
            r = svc.events().list(calendarId=cal, timeMin=f"{a.isoformat()}T00:00:00Z", timeMax=f"{b.isoformat()}T00:00:00Z",
                                  singleEvents=True, pageToken=page, maxResults=250).execute()
            out += r.get("items", [])
            page = r.get("nextPageToken")
            if not page:
                break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--since", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    if not cfg.get("mirror_dir"):
        print("timeline mirror: mirror_dir unset; nothing done"); return
    root = Path(cfg["mirror_dir"]).expanduser() / (cfg.get("timeline_dir") or TIMELINE_DIR)
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    today = datetime.now(tz).date()
    lo, hi = today - timedelta(days=a.since), today + timedelta(days=3)
    offsets = sorted({int(cfg.get("traffic_offset_years") or 0), LEGACY_TRAFFIC_OFFSET, TRANSCRIPT_OFFSET} - {0})
    svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    events = list_window(svc, cal, lo, hi, offsets)
    wanted = {}
    for ev in events:
        priv = ev.get("extendedProperties", {}).get("private", {})
        if priv.get("mirror_path") or priv.get("publish_path"):
            continue                                   # the two-way mirror / publisher own these
        if ev.get("status") == "cancelled":
            continue
        start, timed = when(ev)
        day = datetime.fromisoformat(start).astimezone(tz).date() if timed else date.fromisoformat(start)
        real, off = decode(day, offsets)
        end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date") or ""
        text = (ev.get("description") or "").rstrip() + "\n"
        h = body_hash(text + "|" + start + "|" + end + "|" + (ev.get("summary") or ""))
        path = root / f"{real.year}" / real.isoformat() / f"{file_stem(ev.get('summary') or 'event')} · {ev['id'][:6]}.md"
        wanted[path] = frontmatter(ev, real, off, start, end, timed, h) + text
    written = removed = same = 0
    for path, content in wanted.items():
        if path.exists():
            old = path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^event_hash: (\w+)$", old, re.M)
            if m and m.group(1) == re.search(r"^event_hash: (\w+)$", content, re.M).group(1):
                same += 1; continue
        print(("[dry] " if a.dry_run else "") + f"vault <- calendar  {path.relative_to(root)}")
        if not a.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content, encoding="utf-8")
        written += 1
    # files inside the window whose event is gone (deleted or moved) go with it
    if root.exists():
        for p in root.glob("*/*/*.md"):
            try:
                d = date.fromisoformat(p.parent.name)
            except ValueError:
                continue
            if lo <= d < hi and p not in wanted:
                print(("[dry] " if a.dry_run else "") + f"vault  x  removed    {p.relative_to(root)}")
                if not a.dry_run:
                    p.unlink()
                removed += 1
    print(f"timeline pass: {len(wanted)} events in window ({lo}..{hi}), {written} written, {same} unchanged, {removed} removed")


if __name__ == "__main__":
    main()
