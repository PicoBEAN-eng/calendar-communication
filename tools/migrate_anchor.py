#!/usr/bin/env python3
"""Move a stream's context-note layer from one anchor year to another (e.g. 2000 -> 3000).

Every all-day event in the source year is re-dated day-for-day plus the year offset, through
cp.write_event with a FRESH event as `existing`, so location and private properties are merged, not
re-applied stale. All-day events always end the day after they start (day listings and search skip
zero-length ones). --remap moves a whole source day somewhere else instead (e.g. the design notes
that sat on 2000-01-03 onto the index day 3000-01-02). Dry run by default: prints old date, new
date and title for every event; nothing is written without --apply.

After --apply: set note_anchor_date = "<to-year>-01-01" in comms.toml (git-ignored, hand-edited),
run tools/note_protocol.py --apply to rebuild the relay note and Note: Index, and re-paste the phone
profile block. The poller keeps treating "Note:" titles as passive throughout, so no window opens.

  migrate_anchor.py [--from-year 2000] [--to-year 3000] [--remap 2000-01-03=3000-01-02 ...] [--apply]
"""
import argparse, sys
from datetime import date, timedelta
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC))
import comms_poller as cp  # noqa: E402


def shifted(d: str, years: int) -> str:
    return f"{int(d[:4]) + years:04d}{d[4:]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--from-year", type=int, default=2000)
    ap.add_argument("--to-year", type=int, default=3000)
    ap.add_argument("--remap", action="append", default=[], help="SRC-DAY=DEST-DAY, overrides the offset for that day")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config)); svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    offset = a.to_year - a.from_year
    remap = dict(r.split("=", 1) for r in a.remap)
    items, page = [], None
    while True:
        cp.pace()
        r = svc.events().list(calendarId=cal, timeMin=f"{a.from_year}-01-01T00:00:00Z", timeMax=f"{a.from_year + 1}-01-01T00:00:00Z",
                              singleEvents=True, maxResults=250, pageToken=page, orderBy="startTime").execute()
        items += r.get("items", []); page = r.get("nextPageToken")
        if not page:
            break
    if not items:
        print(f"no events in {a.from_year}; nothing to do"); return 0
    plan, skipped = [], []
    for ev in items:
        old = ev["start"].get("date")
        if not old:
            skipped.append((ev.get("summary"), "timed event, not all-day")); continue
        new = remap.get(old) or shifted(old, offset)
        end = (date.fromisoformat(new) + timedelta(days=1)).isoformat()
        plan.append((ev, old, new, end))
    tag = "" if a.apply else "[dry-run] "
    for ev, old, new, end in plan:
        print(f"{tag}{old} -> {new}  {ev.get('summary')}")
    for title, why in skipped:
        print(f"{tag}SKIP {title}: {why}")
    print(f"{tag}{len(plan)} event(s) to move, {len(skipped)} skipped")
    if not a.apply:
        print("dry run; add --apply to write, then set note_anchor_date in comms.toml and run tools/note_protocol.py --apply")
        return 0
    moved = 0
    for ev, old, new, end in plan:
        cp.pace(); fresh = svc.events().get(calendarId=cal, eventId=ev["id"]).execute()
        cp.write_event(svc, cal, ev["id"], {"start": {"date": new}, "end": {"date": end}}, existing=fresh)
        moved += 1
    print(f"moved {moved} event(s) into {a.to_year}. Now: note_anchor_date = \"{a.to_year}-01-01\" in comms.toml; tools/note_protocol.py --apply; re-paste the phone profile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
