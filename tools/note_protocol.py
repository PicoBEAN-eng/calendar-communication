#!/usr/bin/env python3
"""Publish this stream's copy of the phone-side rules as a context note, and rebuild "Note: Index".

"Note: <Stream> relay instructions" = the fenced block of docs/voice-profile.md with <Stream>
substituted and the multi-stream STREAMS line dropped (this note lives on one calendar), so a
fresh voice session that has lost its profile can be pointed at ONE event and read the contract.
"Note: Index" = every Note: event on the anchor date with its one-line summary (first description
line), the instructions note first. Dry-run by default; --apply writes.

  tools/note_protocol.py [--config comms.toml] [--apply]
"""
import argparse, re, sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import comms_poller as cp  # noqa: E402

INDEX_TITLE = "Note: Index"


def protocol_title(stream: str) -> str:
    return f"Note: {stream} relay instructions"


def protocol_summary(stream: str) -> str:
    return (f"How to talk to {stream} through this calendar: request events, gear words, "
            "whiteboard threads, reading replies, context notes. Read this first in a new conversation.")


def protocol_body(stream: str, cfg: dict) -> str:
    doc = (HERE / "docs/voice-profile.md").read_text()
    m = re.search(r"```\n(.*?)```", doc, re.S)
    if not m:
        sys.exit("no fenced block in docs/voice-profile.md")
    block = m.group(1).strip()
    # Drop the STREAMS paragraph (up to the first blank line): this note is one calendar's own copy.
    if block.startswith("STREAMS"):
        block = block.split("\n\n", 1)[1]
    block = block.replace("<Stream>'s", f"{stream}'s").replace("<Stream>", stream)
    # The front door is per stream: substitute this instance's anchor date in every form the block uses.
    a = date.fromisoformat(cfg["note_anchor_date"])
    nxt = date.fromordinal(a.toordinal() + 1)
    block = (block.replace("<AnchorLong>", f"{a.day} {a:%B} {a.year}").replace("<AnchorShort>", f"{a.day} {a:%b} {a.year}")
                  .replace("<AnchorNext>", nxt.isoformat()).replace("<Anchor>", a.isoformat()))
    off = int(cfg.get("traffic_offset_years") or 0)
    band = (f"the same day and time, {off} years forward (today is today plus {off} years there)" if off
            else "the live dates themselves (today and yesterday)")
    block = block.replace("<TrafficBand>", band)
    create = (f"Create the request directly in the traffic band: the same day and time as now, {off} years forward. "
              "Nobody reads the live dates; the calendar is a datastore between agents. A request created on a live date by habit is moved into the band when it is claimed, so nothing is lost either way." if off
              else "Create the request on today's date, at the current time.")
    block = block.replace("<TrafficCreate>", create)
    # The block opens with the arrival paragraphs (recall, not briefing); the summary line lives in the Index row only.
    body = f"{block}\n\nUpdated {date.today().isoformat()}"
    if len(body) > 8000:
        sys.exit(f"protocol body too long: {len(body)} chars")
    return body


def list_notes(svc, cfg) -> list[dict]:
    anchor = cfg["note_anchor_date"]
    # Days one and two only: the library's chart days (anchor+2 onward) are reached via their own
    # index notes on day two, so they never appear in Note: Index.
    year_end = f"{(date.fromisoformat(anchor) + timedelta(days=2)).isoformat()}T00:00:00Z"
    out, page = [], None
    while True:
        resp = svc.events().list(calendarId=cfg["calendar_id"], timeMin=f"{anchor}T00:00:00Z",
                                 timeMax=year_end, singleEvents=True, pageToken=page, maxResults=250).execute()
        out += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            # timeMax is UTC; an all-day event on day three starts before it in eastern zones,
            # so filter by the calendar date as well.
            last = (date.fromisoformat(anchor) + timedelta(days=1)).isoformat()
            return [ev for ev in out if ev["start"].get("date", ev["start"].get("dateTime", ""))[:10] <= last]


def index_body(notes: list[dict], cfg) -> str:
    stream, anchor = cfg["stream"], cfg["note_anchor_date"]
    ptitle = protocol_title(stream)

    def summary(ev):
        return ((ev.get("description") or "").strip().splitlines() or ["(no summary)"])[0]
    rows = {ev["summary"]: (summary(ev), ev["start"].get("date", anchor)) for ev in notes
            if ev.get("summary", "").lower().startswith("note:") and ev["summary"] != INDEX_TITLE}
    rows[ptitle] = (protocol_summary(stream), anchor)
    ordered = [ptitle] + sorted(t for t in rows if t != ptitle)
    lines = [f"Index of context notes on the {stream} calendar ({len(ordered)} notes, {anchor} and the day after). "
             "Load one by searching its exact title with the day pinned to the date shown. "
             "Deeper library days are reached through the index notes listed here.", ""]
    lines += [f"- {t} — {rows[t][0]}" + ("" if rows[t][1] == anchor else f" ({rows[t][1]})") for t in ordered]
    lines += ["", f"Updated {date.today().isoformat()}"]
    return "\n".join(lines)


def upsert(svc, cfg, existing: dict, title: str, body: str, apply: bool):
    anchor = cfg["note_anchor_date"]
    nxt = date.fromisoformat(anchor).toordinal() + 1
    ev = {"summary": title, "description": body, "start": {"date": anchor},
          "end": {"date": date.fromordinal(nxt).isoformat()},
          "reminders": {"useDefault": False}, "transparency": "transparent",
          "extendedProperties": {"private": {"comms_kind": "note", "comms_writer": "note_protocol"}}}
    verb = "update " if title in existing else "insert "
    if apply:
        if title in existing:
            svc.events().update(calendarId=cfg["calendar_id"], eventId=existing[title], body=ev).execute()
        else:
            svc.events().insert(calendarId=cfg["calendar_id"], body=ev).execute()
    print(f"{'' if apply else '[dry-run] '}{verb}{title}  ({len(body)} chars)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "comms.toml"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(Path(a.config))
    svc = cp.get_service(cfg)
    notes = list_notes(svc, cfg)
    existing = {ev.get("summary"): ev["id"] for ev in notes}
    proto = protocol_body(cfg["stream"], cfg)
    upsert(svc, cfg, existing, protocol_title(cfg["stream"]), proto, a.apply)
    upsert(svc, cfg, existing, INDEX_TITLE, index_body(notes, cfg), a.apply)
    if not a.apply:
        print("\n===== " + protocol_title(cfg["stream"]) + "\n" + proto + "\n\n===== " + INDEX_TITLE + "\n" + index_body(notes, cfg))
