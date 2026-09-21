#!/usr/bin/env python3
"""Move whole layer years of this stream's calendar to new years (the band move, 2026-09-22).

  migrate_layers.py --map 3000=2040,3010=2041,... [--sweep-dir <vault>] [--apply]

Every event whose start falls in an old year is re-dated day for day into the new year (all-day events
keep end = start + 1; timed events keep their clock time). Writes go through cp.write_event with a fresh
event, so location, private properties (keys, mirror memory) survive; keys, links and mirror paths are
date-free. With --sweep-dir, dates written inside note bodies ("3030-01-01", "1 January 3030", "year
3030") are rewritten in every mirrored note on both sides (calendar description + vault file, hash
updated) so the mirror sees them as in sync. Dry run by default: prints counts per year.
"""
import argparse, re, sys
from datetime import date, timedelta
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools"))
import comms_poller as cp  # noqa: E402
import mirror as M  # noqa: E402

MONTHS = "January February March April May June July August September October November December".split()


def shift(iso: str, old: str, new: str) -> str:
    return new + iso[len(old):]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True, help="OLD=NEW,OLD=NEW year pairs")
    ap.add_argument("--sweep-dir", help="vault root: rewrite layer dates inside mirrored note bodies (both sides)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    dry = not a.apply; tag = "[dry] " if dry else ""
    ymap = dict(p.split("=") for p in a.map.split(","))
    cfg = cp.load_config(Path(a.config)); svc = cp.get_service(cfg); cal = cfg["calendar_id"]

    moved = {}
    for old, new in ymap.items():
        items, page = [], None
        while True:
            r = svc.events().list(calendarId=cal, timeMin=f"{int(old) - 1}-12-30T00:00:00Z", timeMax=f"{int(old) + 1}-01-02T00:00:00Z",
                                  singleEvents=True, maxResults=2500, pageToken=page).execute()
            items += r.get("items", []); page = r.get("nextPageToken")
            if not page:
                break
        items = [e for e in items if (e["start"].get("date") or e["start"].get("dateTime", ""))[:4] == old]
        moved[old] = len(items)
        for e in items:
            if e["start"].get("date"):
                s = shift(e["start"]["date"], old, new)
                body = {"start": {"date": s}, "end": {"date": (date.fromisoformat(s) + timedelta(days=1)).isoformat()}}
            else:
                body = {"start": {**e["start"], "dateTime": shift(e["start"]["dateTime"], old, new)},
                        "end": {**e["end"], "dateTime": shift(e["end"]["dateTime"], old, new)}}
            if not dry:
                cp.write_event(svc, cal, e["id"], body, existing=e, verify=False)
        print(f"{tag}{old} -> {new}: {len(items)} events")

    if a.sweep_dir:
        vault = Path(a.sweep_dir).expanduser()
        pats = []
        for old, new in ymap.items():
            pats.append((re.compile(rf"\b{old}(-\d\d-\d\d)\b"), lambda m, new=new: new + m.group(1)))
            pats.append((re.compile(rf"\b(\d{{1,2}} (?:{'|'.join(MONTHS)}) ){old}\b"), lambda m, new=new: m.group(1) + new))
            pats.append((re.compile(rf"\b(year |years |in |on |at |layer |to |from ){old}\b"), lambda m, new=new: m.group(1) + new))
            pats.append((re.compile(rf"\b{old}(?=-\d\d\b|\b)(?![-\d])"), lambda m, new=new: new))

        def sweep(text: str) -> str:
            out = text
            for rx, fn in pats:
                out = rx.sub(fn, out)
            return out
        events = M.list_future(svc, cal)
        changed = 0
        for e in events:
            rel = e.get("extendedProperties", {}).get("private", {}).get("mirror_path")
            if not rel:
                continue
            desc = e.get("description") or ""
            new = sweep(desc)
            if new == desc:
                continue
            changed += 1
            print(f"{tag}sweep   {e['summary']}")
            if not dry:
                cp.write_event(svc, cal, e["id"], {"description": new, "extendedProperties": {"private": {"mirror_hash": M.h(M.canonical(M.join_parts([new], None, None)))}}},
                               existing=e, verify=False)
                f = vault / rel
                if f.exists():
                    f.write_text(sweep(f.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"{tag}sweep: {changed} notes with layer dates in their text")
    print(f"{tag}moved: {moved}")


if __name__ == "__main__":
    main()
