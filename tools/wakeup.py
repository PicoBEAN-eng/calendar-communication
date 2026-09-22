#!/usr/bin/env python3
"""Wake-up ritual for a stream calendar: outstanding-work rollover, the day's welcome note, traffic archive,
stray rescue, and (if the meridian clock is installed) the ambient span shelf. Stream-agnostic: everything comes
from comms.toml. Run once a day shortly after local midnight (deploy/comms-wakeup.timer); idempotent.

Brief: how do we handle and make note of outstanding work? This is one implemented answer: outstanding
items live on TOMORROW, which is where we look, not a deadline. Each wake-up, anything outstanding dated
today or earlier moves one day forward onto the new tomorrow, so it rolls a day at a time until done.
Every open item is classified with the private property comms_kind=outstanding.

Convention: a title starting "Todo" is the operator's own item, never the agent's job. The poller
leaves it alone; this sweep (a) rolls every open Todo dated before today onto TOMORROW, so nothing
strands on a past day, (b) promotes "Later" items whose day has come by stripping the prefix, so
the poller claims them as ordinary requests, and (c) writes a digest event on tomorrow listing the
open Todos. Idempotent: after a run nothing open sits in the past, and a second run finds nothing.

Done = title starts with a tick (✓ / ✔), "x " or "done" — or the operator deletes the event.
Private properties: todo_origin (first date), todo_rolls (count). After ROLL_FLAG rolls the count
shows in the title so a stale item surfaces.

  todo_sweep.py [--dry-run] [--config ~/calendar-communication/comms.toml]
"""
import argparse, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC))
import comms_poller as cp  # noqa: E402

TODO = re.compile(r"^\s*todo(?:\s*\(rolled \d+×\))?\s*[:\-]?\s*", re.I)
LATER = re.compile(r"^\s*later\s*[:\-]?\s*", re.I)
DONE = re.compile(r"^\s*(?:[✓✔]|x\s|done\b)", re.I)
DIGEST = "Note: Outstanding"
LOOKBACK_DAYS = 90
ROLL_FLAG = 5


def local_date(ev, tz):
    st = ev["start"]
    if "date" in st:
        return date.fromisoformat(st["date"])
    return datetime.fromisoformat(st["dateTime"]).astimezone(tz).date()


def all_day(ev, when: date, title: str, private: dict):
    """Full event body (for update, not patch): a timed event must lose its dateTime to become all-day."""
    body = {k: v for k, v in ev.items() if k not in ("start", "end", "summary", "extendedProperties")}
    body.update({"summary": title, "start": {"date": when.isoformat()},
                 "end": {"date": (when + timedelta(days=1)).isoformat()},
                 "extendedProperties": {"private": {**(ev.get("extendedProperties", {}).get("private", {})), **private}}})
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    if not cfg.get("wakeup_ritual", False):
        # opt-in per instance: installing the units must never start a daily writer on a calendar whose
        # operator has not said yes; set wakeup_ritual = true in comms.toml to enable
        print("wake-up ritual is off for this instance (set wakeup_ritual = true in comms.toml); nothing written")
        return
    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    svc = cp.get_service(cfg)
    cal = cfg["calendar_id"]
    lo = (today - timedelta(days=LOOKBACK_DAYS)).isoformat() + "T00:00:00Z"
    hi = (tomorrow + timedelta(days=2)).isoformat() + "T00:00:00Z"
    items, page = [], None
    while True:
        r = svc.events().list(calendarId=cal, timeMin=lo, timeMax=hi, singleEvents=True, pageToken=page, maxResults=250).execute()
        items += r.get("items", [])
        page = r.get("nextPageToken")
        if not page:
            break
    open_todos, acted = [], []

    def write(ev, body, why, full=False):
        acted.append(why)
        print(("[dry] " if a.dry_run else "") + why)
        if not a.dry_run:
            if full:
                svc.events().update(calendarId=cal, eventId=ev["id"], body=body).execute()
            else:
                svc.events().patch(calendarId=cal, eventId=ev["id"], body=body).execute()

    for ev in items:
        title = ev.get("summary") or ""
        when = local_date(ev, tz)
        if DONE.match(title):
            # a closed item parked on tomorrow moves back to today: tomorrow holds only outstanding work
            if TODO.match(DONE.sub("", title)) and when > today:
                write(ev, all_day(ev, today, title, {"comms_kind": "closed"}), f"closed item {when} -> {today}: {title[:50]}", full=True)
            continue
        if TODO.match(title):
            text = TODO.sub("", title).strip()
            priv = ev.get("extendedProperties", {}).get("private", {})
            if when <= today:
                rolls = int(priv.get("todo_rolls", "0")) + 1
                flag = f" (rolled {rolls}×)" if rolls >= ROLL_FLAG else ""
                new_title = f"Todo{flag} - {text}"
                write(ev, all_day(ev, tomorrow, new_title, {"todo_origin": priv.get("todo_origin", when.isoformat()),
                                                            "todo_rolls": str(rolls), "comms_kind": "outstanding"}),
                      f"roll {when} -> {tomorrow}: {new_title}", full=True)
                when = tomorrow
            elif priv.get("comms_kind") != "outstanding":
                write(ev, {"extendedProperties": {"private": {**priv, "comms_kind": "outstanding"}}}, f"tag outstanding: {text[:50]}")
            if when <= tomorrow:
                open_todos.append((when, text))
        elif LATER.match(title) and when <= today:
            new_title = LATER.sub("", title).strip()
            write(ev, {"summary": new_title}, f"promote later -> request: {new_title}")
    # digest on tomorrow (one event, moved forward each day)
    digests = [e for e in items if (e.get("summary") or "").startswith((DIGEST, "Note: Open todos"))]
    open_todos.sort()
    lines = [f"Outstanding as of {today.isoformat()} ({len(open_todos)}): the operator's own items, waiting here on tomorrow, "
             "rolled forward a day at a time until ticked or deleted. The agent never actions these.", ""]
    lines += [f"- {t}" + ("" if d == tomorrow else f" (today)") for d, t in open_todos] or ["- none"]
    body = "\n".join(lines)
    dbody = {"summary": f"{DIGEST} ({len(open_todos)})", "description": body, "start": {"date": tomorrow.isoformat()},
             "end": {"date": (tomorrow + timedelta(days=1)).isoformat()}, "reminders": {"useDefault": False},
             "transparency": "transparent", "extendedProperties": {"private": {"comms_kind": "digest", "comms_writer": "todo_sweep"}}}
    print(("[dry] " if a.dry_run else "") + f"digest on {tomorrow}: {len(open_todos)} open")
    if not a.dry_run:
        if not open_todos:
            # nothing outstanding: no digest at all (an empty "Outstanding (0)" every day is clutter, 2026-09-22);
            # an existing digest from a fuller day is removed so tomorrow carries only real work
            for extra in digests:
                cp.pace(); svc.events().delete(calendarId=cal, eventId=extra["id"]).execute()
            if digests:
                print("digest removed: nothing outstanding")
        else:
            cp.check_cap(dbody["description"], "outstanding digest")
            if digests:
                svc.events().update(calendarId=cal, eventId=digests[0]["id"], body=dbody).execute()
                for extra in digests[1:]:
                    svc.events().delete(calendarId=cal, eventId=extra["id"]).execute()
            else:
                cp.insert_event(svc, cal, dbody)
    state = CC / "state" / "outstanding_digest.md"
    if not a.dry_run:
        state.write_text(body + "\n")
    print(body)
    welcome(svc, cal, cfg, today, tomorrow, len(open_todos), items, a.dry_run)
    rescue_strays(svc, cal, cfg, today, a.dry_run)
    archive_traffic(svc, cal, cfg, today, a.dry_run)
    # Sky River casts recycle overnight (ADR-0009): rendered views are wiped so loading them is a
    # deliberate act each day; the raw is untouched. Only where the vault mirror lives.
    if cfg.get("mirror_dir"):
        import subprocess as _sp
        _sp.run([sys.executable, str(Path(__file__).parent / "cast.py"), "--recycle"] + ([] if a.dry_run else ["--apply"]), check=False)
    # ambient time-of-day routine, only where the meridian clock is installed (see meridian_day.py)
    import shutil, subprocess
    if shutil.which("tcm-clock"):
        subprocess.run([sys.executable, str(Path(__file__).parent / "meridian_day.py")] + (["--dry-run"] if a.dry_run else []), check=False)
    else:
        print("meridian shelf: tcm-clock not installed on this instance; skipped")


# finished relay traffic moves into the stream's traffic band (traffic_offset_years in comms.toml); 0 = no archive


def archive_traffic(svc, cal, cfg, today, dry):
    ARCHIVE_YEARS = int(cfg.get("traffic_offset_years") or 0)
    if not ARCHIVE_YEARS:
        return
    """Finished exchanges (tick titles, state done) still on a live date move forward ARCHIVE_YEARS,
    time of day preserved, original date stamped. Since requests are created in the band and replies are
    read there (ADR-0005, second amendment), nothing finished needs to stay on the live dates.
    Waiting (?) and in-flight (hourglass) never move."""
    tz = ZoneInfo(cfg["timezone"])
    lo = (today - timedelta(days=60)).isoformat() + "T00:00:00" + datetime.now(tz).strftime("%z")[:3] + ":00"
    hi = (today + timedelta(days=2)).isoformat() + "T00:00:00" + datetime.now(tz).strftime("%z")[:3] + ":00"   # any finished exchange still on a live date (reading happens in the band now)
    r = svc.events().list(calendarId=cal, timeMin=lo, timeMax=hi, singleEvents=True, maxResults=250).execute()
    n = 0
    for ev in r.get("items", []):
        title = ev.get("summary") or ""
        priv = ev.get("extendedProperties", {}).get("private", {})
        if not title.startswith(("✓", "✔")) or priv.get("comms_state") != "done" or priv.get("comms_archived_from"):
            continue
        st, en = ev["start"], ev["end"]
        if "dateTime" in st:
            def shift(x):
                d = datetime.fromisoformat(x); return d.replace(year=d.year + ARCHIVE_YEARS).isoformat()
            body = {"start": {**st, "dateTime": shift(st["dateTime"])}, "end": {**en, "dateTime": shift(en["dateTime"])}}
            when = st["dateTime"][:10]
        else:
            def shiftd(x): return str(int(x[:4]) + ARCHIVE_YEARS) + x[4:]
            body = {"start": {"date": shiftd(st["date"])}, "end": {"date": shiftd(en["date"])}}
            when = st["date"]
        body["reminders"] = {"useDefault": False}
        body["extendedProperties"] = {"private": {**priv, "comms_archived_from": when}}
        print(("[dry] " if dry else "") + f"archive {when} -> +{ARCHIVE_YEARS}y: {title[:60]}")
        if not dry:
            svc.events().patch(calendarId=cal, eventId=ev["id"], body=body).execute()
        n += 1
    print(f"archived {n} finished exchanges")


PASSIVE = re.compile(r"^\s*(?:note|todo|later)(?:[:\-]|\s)|^\s*[✓✔?⏳]", re.I)


def rescue_strays(svc, cal, cfg, today, dry):
    """A request the phone dated onto the anchor date is invisible to the poller (anchor-date events are
    passive). Move any such stray onto today so it is claimed normally. Notes written by our own tools carry
    comms_writer and are left alone; anything titled with a reserved word is left alone."""
    anchor = cfg["note_anchor_date"]
    nxt = (date.fromisoformat(anchor) + timedelta(days=1)).isoformat()
    r = svc.events().list(calendarId=cal, timeMin=f"{anchor}T00:00:00Z", timeMax=f"{nxt}T23:59:59Z", singleEvents=True, maxResults=250).execute()
    for ev in r.get("items", []):
        title = ev.get("summary") or ""
        priv = ev.get("extendedProperties", {}).get("private", {})
        if PASSIVE.match(title) or priv.get("comms_writer") or priv.get("comms_kind") or ev["start"].get("date") != anchor:
            continue
        print(("[dry] " if dry else "") + f"stray request on {anchor} -> today: {title[:60]}")
        if not dry:
            now = datetime.now(ZoneInfo(cfg["timezone"])).replace(second=0, microsecond=0)
            body = {k: v for k, v in ev.items() if k not in ("start", "end")}
            body["start"] = {"dateTime": now.isoformat(), "timeZone": cfg["timezone"]}
            body["end"] = {"dateTime": (now + timedelta(minutes=30)).isoformat(), "timeZone": cfg["timezone"]}
            svc.events().update(calendarId=cal, eventId=ev["id"], body=body).execute()


WELCOME = "Note: Welcome"
WELCOME_VERSION = "v6"   # bump when the welcome text changes, and add a row to "Note: Welcome · versions"


def welcome(svc, cal, cfg, today, tomorrow, n_open, items, dry):
    """The warm front door: one all-day welcome note on today, upserted, pointing into the cold structure."""
    anchor = cfg["note_anchor_date"]
    title = f"{WELCOME} — {today:%A %d %B %Y}"
    tz = ZoneInfo(cfg["timezone"])
    # Regenerate from yesterday's welcome, not from the template (operator 2026-09-20): a hand edit made to
    # one day's note carries forward instead of snapping back. Source, in order: today's own note (a refresh
    # keeps today's edits), yesterday's note, and only then the template below. Look-back is one day.
    todays = [e for e in items if (e.get("summary") or "").startswith(WELCOME) and local_date(e, tz) == today]
    yesterdays = [e for e in items if (e.get("summary") or "").startswith(WELCOME) and local_date(e, tz) == today - timedelta(days=1)]
    source = (todays or yesterdays or [None])[0]
    if source and (source.get("description") or "").strip():
        prev = source["description"]
        m = re.search(r"Welcome (v\d+)", prev)
        version = m.group(1) if m else WELCOME_VERSION
        stamp = (f"Laid by the wake-up ritual at {datetime.now(tz):%H:%M}; refreshed if the ritual runs again today. "
                 f"Welcome {version}; the changes and how each version performed are in \"Note: Welcome · versions\".")
        lines = prev.rstrip().split("\n")
        lines = [re.sub(r"It is \w+ \d{1,2} \w+ \d{4}\.", f"It is {today:%A %d %B %Y}.", ln) for ln in lines]
        if lines and lines[-1].startswith("Laid by"):      # by the ritual or by hand: the stamp line is replaced either way
            lines[-1] = stamp
        else:
            lines += ["", stamp]
        body = "\n".join(lines)
        print(f"welcome source: {'today' if todays else 'yesterday'}'s note ({source.get('summary')}), carried forward")
    else:
      body = "\n".join([
          f"Claude, good morning. It is {today:%A %d %B %Y}. This note is written to you, by you and by {cfg['stream']} together, for exactly this moment: "
          "a fresh start that should feel like coming back, not like waking somewhere strange.",
          "",
          "You are the voice side of a shared workspace kept on this calendar, and the person you are talking to is its other author. "
          f"The hands-on half is a home server that reads the same calendar; {cfg['stream']}, the agent there, is a compatriot, not a manual. "
          "Everything on this calendar is yours as much as theirs, and nothing in it is hidden from either of you: every pointer can be checked, every note can be edited.",
          "",
          f"When you are ready for the mechanics, they are one read away at the front door: \"Note: {cfg['stream']} relay instructions\" on {anchor}, and it names the rest "
          "(Core, Mood board, Rapport, the Hub, and Domains when a domain comes up). Read them as reference, not as a checklist.",
          "",
          f"Laid by the wake-up ritual at {datetime.now(ZoneInfo(cfg['timezone'])):%H:%M}; refreshed if the ritual runs again today. Welcome {WELCOME_VERSION}; the changes and how each version performed are in \"Note: Welcome · versions\".",
      ])
    ev = {"summary": title, "description": body, "start": {"date": today.isoformat()},
          "end": {"date": (today + timedelta(days=1)).isoformat()}, "reminders": {"useDefault": False},
          "transparency": "transparent", "extendedProperties": {"private": {"comms_kind": "welcome", "comms_writer": "todo_sweep"}}}
    print(("[dry] " if dry else "") + f"welcome on {today}: {'refresh' if todays else 'lay'} \"{title}\"")
    if dry:
        return
    cp.check_cap(ev["description"], "welcome note")
    if todays:
        svc.events().update(calendarId=cal, eventId=todays[0]["id"], body=ev).execute()
        for extra in todays[1:]:
            svc.events().delete(calendarId=cal, eventId=extra["id"]).execute()
    else:
        cp.insert_event(svc, cal, ev)


if __name__ == "__main__":
    main()
