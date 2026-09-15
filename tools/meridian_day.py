#!/usr/bin/env python3
"""Ambient time-of-day routine: write the day's TCM meridian windows as calendar SPAN events.

Option 2 of the design fork (2026-09-11): one event per meridian lasting the whole span, start at the
handover, end at the next handover, so "what is active now" = "which events overlap this instant".
(Option 1, a marker at each crossover instant, is noted and not built; revisit.)

Placement: the spans keep their real clock times but are SHELVED on an offset day (OFFSET_DAYS ahead) so
the morning boot read of today is not swamped; to know what is active now, read the shelf day at this clock
time. Spans that have drifted onto today (written OFFSET_DAYS ago) are removed each run, so today stays
clean. OFFSET_DAYS = 0 would put them at their real instants; a dedicated ambient calendar would be cleaner
still (needs the operator to create one). Class key in the location field per P2; no per-entry keys, the
spans are ephemeral.

  meridian_day.py [--dry-run] [--for YYYY-MM-DD]
"""
import argparse, json, subprocess, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_poller as cp  # noqa: E402

TOOL = Path.home() / ".local/bin/tcm-clock"
CLASS_KEY = None   # minted per instance on first run (tools/keys.py)
OFFSET_DAYS = 2
TITLE = "Note: Meridian"


def schedule():
    out = subprocess.run([str(TOOL), "--json"], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def spans_for(day: date, tz):
    d = schedule()
    base = datetime(day.year, day.month, day.day, tzinfo=tz)
    for m in d["schedule"]:
        start = base + timedelta(minutes=m["start_mins"])
        end = base + timedelta(minutes=m["end_mins"])
        if m["end_mins"] < m["start_mins"]:        # wraps midnight (the Zi window): place it as tonight's, ending after
            end += timedelta(days=1)                 # midnight, so a set never starts before its own shelf day
        yield m, start, end, d


def event_body(m, start, end, d, for_day, tz):
    title = f"{TITLE} · {m['organ']} {m['branch']} {m['start']}–{m['end']}"
    desc = "\n".join([
        f"Meridian span for {for_day.isoformat()}: {m['organ']} ({m['branch']} {m['pinyin']}), {m['element']}, {m['phase']}, {round(m['duration'])} min.",
        m.get("notes", ""),
        "",
        f"Sun that day: rise {d['sunrise']}, noon {d['solar_noon']}, set {d['sunset']}; day half {round(d['day_half_mins'])} min, night half {round(d['night_half_mins'])} min.",
        f"Shelved {OFFSET_DAYS} days ahead so the morning boot stays clean; the clock times are real. Ambient note, not a request; class key in the location field.",
    ])
    return {"summary": title, "description": desc, "location": CLASS_KEY,
            "start": {"dateTime": (start + timedelta(days=OFFSET_DAYS)).isoformat(), "timeZone": str(tz)},
            "end": {"dateTime": (end + timedelta(days=OFFSET_DAYS)).isoformat(), "timeZone": str(tz)},
            "reminders": {"useDefault": False}, "transparency": "transparent",
            "extendedProperties": {"private": {"comms_kind": "ambient", "comms_writer": "meridian_day", "ambient_for": for_day.isoformat()}}}


def existing_spans(svc, cal, lo: datetime, hi: datetime):
    r = svc.events().list(calendarId=cal, q=CLASS_KEY, timeMin=lo.isoformat(), timeMax=hi.isoformat(), singleEvents=True, maxResults=250).execute()
    return [e for e in r.get("items", []) if (e.get("summary") or "").startswith(TITLE)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--for", dest="for_day")
    a = ap.parse_args()
    cfg = cp.load_config(CC / "comms.toml")
    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    for_day = date.fromisoformat(a.for_day) if a.for_day else today
    svc = cp.get_service(cfg)
    cal = cfg["calendar_id"]
    global CLASS_KEY
    import keys as K
    CLASS_KEY = K.get_or_mint(svc, cfg, "meridian_class")
    # 1. clear the shelf for this day's window (idempotent), and any stale spans that have drifted onto today
    shelf_lo = datetime(for_day.year, for_day.month, for_day.day, tzinfo=tz) + timedelta(days=OFFSET_DAYS) - timedelta(hours=2)
    shelf_hi = shelf_lo + timedelta(days=1, hours=4)
    stale_lo = datetime(today.year, today.month, today.day, tzinfo=tz) - timedelta(hours=2)
    stale_hi = stale_lo + timedelta(days=1, hours=2)
    # clear this day's own set by its tag, never by time window (a window margin catches the neighbouring set's edge spans)
    victims = {e["id"]: e for e in existing_spans(svc, cal, shelf_lo - timedelta(days=1), shelf_hi + timedelta(days=1))
               if e.get("extendedProperties", {}).get("private", {}).get("ambient_for") == for_day.isoformat()}
    if OFFSET_DAYS:
        # stale = a set whose shelf day has arrived (written OFFSET_DAYS ago or earlier); never a newer set's
        # midnight-straddling spans that merely start inside today's window
        cutoff = (today - timedelta(days=OFFSET_DAYS)).isoformat()
        for e in existing_spans(svc, cal, stale_lo, stale_hi):
            if e.get("extendedProperties", {}).get("private", {}).get("ambient_for", "") <= cutoff:
                victims[e["id"]] = e
    for e in victims.values():
        print(("[dry] " if a.dry_run else "") + f"remove {e['start'].get('dateTime','')[:16]} {e['summary']}")
        if not a.dry_run:
            svc.events().delete(calendarId=cal, eventId=e["id"]).execute()
    # 2. write the twelve spans
    n = 0
    for m, start, end, d in spans_for(for_day, tz):
        body = event_body(m, start, end, d, for_day, tz)
        print(("[dry] " if a.dry_run else "") + f"span {body['start']['dateTime'][:16]} -> {body['end']['dateTime'][11:16]}  {body['summary']}")
        if not a.dry_run:
            svc.events().insert(calendarId=cal, body=body).execute()
        n += 1
    print(f"{n} spans for {for_day} shelved on {for_day + timedelta(days=OFFSET_DAYS)}")


if __name__ == "__main__":
    main()
