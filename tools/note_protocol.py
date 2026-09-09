#!/usr/bin/env python3
"""Publish the relay protocol as a context note + rebuild "Note: Index".

"Note: Woolly relay instructions" = the fenced block of docs/voice-profile.md, so a fresh voice
session that has lost its profile instruction can be pointed at ONE event and read the contract.
"Note: Index" = every Note: event on the anchor date with its one-line summary (first description
line), the instructions note first. Dry-run by default; --apply writes.
"""
import argparse, re, sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import comms_poller as cp  # noqa: E402

ANCHOR = "2000-01-01"
PROTOCOL_TITLE = "Note: Woolly relay instructions"
INDEX_TITLE = "Note: Index"
PROTOCOL_SUMMARY = ("How to talk to Woolly through this calendar: request events, tier words, "
                    "whiteboard threads, reading replies, context notes. Read this first in a new conversation.")


def protocol_body() -> str:
    doc = (HERE / "docs/voice-profile.md").read_text()
    m = re.search(r"```\n(.*?)```", doc, re.S)
    if not m:
        sys.exit("no fenced block in docs/voice-profile.md")
    block = m.group(1).strip()
    body = (f"{PROTOCOL_SUMMARY}\n\n"
            "You are the voice side of the Woolly relay. Follow these instructions for the rest of this conversation.\n\n"
            f"{block}\n\nUpdated {date.today().isoformat()}")
    if len(body) > 8000:
        sys.exit(f"protocol body too long: {len(body)} chars")
    return body


def list_notes(svc, cfg) -> list[dict]:
    out, page = [], None
    while True:
        resp = svc.events().list(calendarId=cfg["calendar_id"], timeMin=f"{ANCHOR}T00:00:00Z",
                                 timeMax="2001-01-01T00:00:00Z", singleEvents=True, pageToken=page,
                                 maxResults=250).execute()
        out += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            return out


def index_body(notes: list[dict]) -> str:
    def summary(ev):
        return ((ev.get("description") or "").strip().splitlines() or ["(no summary)"])[0]
    rows = {ev["summary"]: (summary(ev), ev["start"].get("date", ANCHOR)) for ev in notes
            if ev.get("summary", "").lower().startswith("note:") and ev["summary"] != INDEX_TITLE}
    rows[PROTOCOL_TITLE] = (PROTOCOL_SUMMARY, ANCHOR)
    ordered = [PROTOCOL_TITLE] + sorted(t for t in rows if t != PROTOCOL_TITLE)
    lines = [f"Index of context notes on the Woolly calendar ({len(ordered)} notes, year 2000). "
             "Load one by searching its exact title with the day pinned to the date shown. "
             "Once the library exists, a reader scans days one and two (1–2 Jan 2000) only.", ""]
    lines += [f"- {t} — {rows[t][0]}" + ("" if rows[t][1] == ANCHOR else f" ({rows[t][1]})") for t in ordered]
    lines += ["", f"Updated {date.today().isoformat()}"]
    return "\n".join(lines)


def upsert(svc, cfg, existing: dict, title: str, body: str, apply: bool):
    ev = {"summary": title, "description": body, "start": {"date": ANCHOR}, "end": {"date": "2000-01-02"},
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
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    cfg = cp.load_config(HERE / "comms.toml")
    svc = cp.get_service(cfg)
    notes = list_notes(svc, cfg)
    existing = {ev.get("summary"): ev["id"] for ev in notes}
    proto = protocol_body()
    upsert(svc, cfg, existing, PROTOCOL_TITLE, proto, a.apply)
    upsert(svc, cfg, existing, INDEX_TITLE, index_body(notes), a.apply)
    if not a.apply:
        print("\n===== " + PROTOCOL_TITLE + "\n" + proto + "\n\n===== " + INDEX_TITLE + "\n" + index_body(notes))
