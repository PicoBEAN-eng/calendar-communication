#!/usr/bin/env python3
"""calendar-communication poller — the Google Calendar side of the comms.

One stream calendar (a secondary calendar on the Gmail account) is the message bus:

  request   : an event the phone created (no state yet, no title prefix)
  claimed   : we saw it → "⏳ " prefix, yellow, inbox file written for the session
  done      : session replied → "✓ " prefix, green, the description IS the reply (whiteboard
              slot: one turn at a time), event slid to now + popup + lead with a single 10-minute
              popup so the phone buzzes and the notification lingers
  question  : session needs input → "? " prefix, red, same treatment
  progress  : session heartbeat → "⏳ <text> · " in the TITLE only; the description (the
              user's question) is never touched
  new turn  : an answered event whose description no longer equals the text we last wrote
              (and is not empty) is the next turn of the same thread — re-claimed, turn+1

Whiteboard model (2026-09-08): the event holds only the current turn; the durable memory is
the per-thread transcript file spool/threads/<root_event_id>.md (both sides appended every
turn), whose path rides in the inbox file so the session reads it before answering.
Replies are written with an etag conditional patch: on a 412 conflict the event is re-read
and, if the phone wrote a newer turn, that turn is claimed with the undelivered reply attached.
The RESOLVED gear the voice side named — a model and an effort, e.g. "opus xhigh" in the title or
"[opus xhigh]" leading the description — is stamped into the inbox file for the channel shim's
sequencer; a silent event gets default_model/default_effort. The poller never classifies.

State lives in the event's private extended properties (invisible on the phone) plus a
small local state file (sync token + processed ids).  The session side talks to us only
through the spool directory:

  spool/inbox/<event_id>.json    request for the session (the channel shim picks it up)
  spool/outbox/<event_id>.json   reply from the session  {"event_id","status","text"}

Commands
  --auth            one-time OAuth consent (paste-the-redirect-URL flow, works headless)
  --list-calendars  show calendar ids (to fill comms.toml)
  --once            one poll: pull changes, claim new requests, push pending replies
  --dry-run         with --once: report what would be claimed / detected, write nothing
  --reply ID --status done|question|progress --text "…"   hand-written reply (testing)
  --show ID         dump one event
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
CONTRACT = 1  # spool/title/notes contract version — see README "Contracts"
PREFIX = {"claimed": "⏳ ", "done": "✓ ", "question": "? "}
COLOR = {"claimed": "5", "done": "10", "question": "11"}  # Banana / Basil / Tomato
PROGRESS_PREFIX = "⏳ "
PROGRESS_SEP = " · "
# RESOLVED-GEAR CONTRACT (2026-09-09): the calendar carries a model and an effort outright,
# never a tier word and never a job description the poller has to judge. Classification happens
# on the voice side (typed, or mapped from shorthand by a context note) BEFORE the event is sent.
# Nothing here interprets: only literal model/effort values are recognised, and the spelling
# table normalises how voice transcribes a value — it never turns a topic into a choice.
MODELS = {"default", "opus-1m", "fable", "sonnet", "haiku", "opus"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


# ----------------------------------------------------------------------------- config
def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = path.parent
    cfg.setdefault("stream", "comms")
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("backfill_hours", 0)
    cfg.setdefault("traffic_offset_years", 0)   # >0: a claimed request moves this many years forward, day-for-day (ADR-0005)
    cfg.setdefault("reply_buzz", True)
    cfg.setdefault("reply_buzz_lead_minutes", 1)
    cfg.setdefault("reply_buzz_popup_minutes", 10)
    cfg.setdefault("note_anchor_date", "2000-01-01")
    cfg.setdefault("default_model", "fable")
    cfg.setdefault("default_effort", "medium")
    for key, default in {
        "spool_dir": "spool",
        "state_file": "state/poller_state.json",
        "token_file": "state/token.json",
        "client_secret_file": "state/client_secret.json",
    }.items():
        cfg[key] = str((base / cfg.get(key, default)).expanduser())
    return cfg


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- auth
def get_service(cfg: dict):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = Path(cfg["token_file"])
    if not token_path.exists():
        sys.exit(f"no token at {token_path} — run: comms_poller.py --auth")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            sys.exit("token invalid and not refreshable — run --auth again")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def do_auth(cfg: dict) -> None:
    """Paste-the-redirect flow: no browser needed on this box.

    The redirect goes to http://localhost:8765/ which the browser cannot reach when it
    runs elsewhere — that is fine: the address bar still carries the ?code=…, and pasting
    that whole URL back here completes the exchange.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # localhost http redirect
    secret = Path(cfg["client_secret_file"])
    if not secret.exists():
        sys.exit(f"missing OAuth client secret at {secret} (download from Google Cloud Console)")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    flow.redirect_uri = "http://localhost:8765/"
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("\n1. Open this URL in any browser signed into the Gmail account:\n")
    print(url)
    print("\n2. Approve. The browser will land on a localhost URL that fails to load —")
    print("   copy the FULL address from the address bar and paste it here.\n")
    pasted = input("redirect URL: ").strip()
    flow.fetch_token(authorization_response=pasted)
    tok = Path(cfg["token_file"])
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text(flow.credentials.to_json())
    os.chmod(tok, 0o600)
    print(f"token saved to {tok}")


# ----------------------------------------------------------------------------- state
def load_state(cfg: dict) -> dict:
    p = Path(cfg["state_file"])
    if p.exists():
        return json.loads(p.read_text())
    return {"sync_token": None, "processed": {}}


def save_state(cfg: dict, state: dict) -> None:
    p = Path(cfg["state_file"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(p)


# ----------------------------------------------------------------------------- helpers
def strip_prefix(summary: str) -> str:
    for pre in PREFIX.values():
        if summary.startswith(pre):
            return summary[len(pre):]
    return summary


def comms_state(ev: dict) -> str | None:
    return (ev.get("extendedProperties") or {}).get("private", {}).get("comms_state")


def now_iso(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")


# "todo" = the operator's own to-do (rolled forward daily by the sweep, never the agent's job);
# "later" = agent work deferred to its date (the sweep strips the prefix on the day, then it is claimed).

# ---------- shared writer helper (2026-09-17) ----------
# Every tool that writes a description goes through here. Three guards in one place:
#   cap:   Google accepts a description longer than 8192 chars and SILENTLY truncates it to 8192 (measured);
#          we refuse to write over the cap and detect a read-back of exactly 8192 as the truncation signature.
#   pace:  one per-user quota of ~400 requests/min is shared by the poller, the ritual and the mirror; each
#          process paces itself to PACE_PER_MIN so a burst never starves the relay.
#   merge: a full-event update wipes location and private properties; write_event always re-reads and merges
#          them unless the caller passes replace_meta=True.
DESCRIPTION_CAP = 8192
PACE_PER_MIN = 200
_pace = {"stamps": []}


class CapExceeded(Exception):
    pass


def check_cap(text: str, where: str = "") -> str:
    if text is not None and len(text) > DESCRIPTION_CAP:
        raise CapExceeded(f"{where or 'description'}: {len(text)} chars exceeds the {DESCRIPTION_CAP} cap; split it (part n of m) instead of writing")
    return text


def pace():
    """Sleep just enough to stay under PACE_PER_MIN calls in any rolling minute."""
    import time as _t
    now = _t.time()
    st = [t for t in _pace["stamps"] if now - t < 60]
    if len(st) >= PACE_PER_MIN:
        _t.sleep(max(0.0, 60 - (now - st[0])))
        now = _t.time(); st = [t for t in st if now - t < 60]
    st.append(now); _pace["stamps"] = st


def merge_location(old: str, new: str) -> str:
    """Location grammar (agreed 2026-09-19): space-separated, order-free tokens. A bare 5-char key is
    identity, p<key> the immediate parent, c<key> a class, name=value a routing token, anything else
    free text. Merging keeps the old tokens except where the new location speaks for the same slot:
    a new p-token replaces the old p-token, a new name=value replaces the old one with that name;
    everything else is a union, new tokens after old, no duplicates."""
    def slot(tok):
        if len(tok) == 6 and tok[0] == "p" and tok[1].isalpha() and tok[1:].isalnum():
            return "p"
        if "=" in tok:
            return "kv:" + tok.split("=", 1)[0]
        return None
    new_toks = (new or "").split()
    taken = {slot(t) for t in new_toks if slot(t)}
    out = [t for t in (old or "").split() if slot(t) not in taken]
    for t in new_toks:
        if t not in out:
            out.append(t)
    return " ".join(out)


def write_event(svc, calendar_id: str, event_id: str, body: dict, *, replace_meta: bool = False,
                verify: bool = True, existing: dict | None = None) -> dict:
    """Patch an event safely: cap-checked, paced, and with location + private properties merged
    (read-merge-write) unless replace_meta. Returns the patched event. Raises CapExceeded before writing."""
    if "description" in body:
        check_cap(body["description"], f"event {event_id}")
    if not replace_meta:
        if existing is None:
            pace(); existing = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        keep = (existing.get("extendedProperties") or {}).get("private") or {}
        given = (body.get("extendedProperties") or {}).get("private") or {}
        body = {**body, "extendedProperties": {"private": {**keep, **given}}}
        if "location" not in body and existing.get("location"):
            body["location"] = existing["location"]
        elif "location" in body and existing.get("location"):
            body["location"] = merge_location(existing["location"], body["location"])
    pace(); ev = svc.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
    if verify and "description" in body and len(ev.get("description") or "") == DESCRIPTION_CAP and len(body["description"]) != DESCRIPTION_CAP:
        raise CapExceeded(f"event {event_id}: read-back is exactly {DESCRIPTION_CAP} chars, the truncation signature")
    return ev


def insert_event(svc, calendar_id: str, body: dict) -> dict:
    if "description" in body:
        check_cap(body["description"], body.get("summary", "new event"))
    pace(); return svc.events().insert(calendarId=calendar_id, body=body).execute()


NOTE_TITLE = re.compile(r"^\s*(?:note|todo|later)(?:[:\-]|\s)", re.IGNORECASE)


def is_note(ev: dict, cfg: dict) -> bool:
    """Passive context note (2026-09-08): title starts with "note" + colon/space/dash (any case),
    or the event sits on the anchor date. Never claimed, coloured, spooled or recorded."""
    if NOTE_TITLE.match(ev.get("summary") or ""):
        return True
    if (ev.get("extendedProperties") or {}).get("private", {}).get("publish_path"):
        return True   # a published vault note is a note whatever its title says (a retitled copy must never be claimed)
    start = ev.get("start") or {}
    when = start.get("date") or start.get("dateTime") or ""
    return when.startswith(cfg["note_anchor_date"])


def strip_progress(summary: str) -> str:
    """'⏳ step 2 of 3 · foo' → 'foo' (a plain claimed prefix is left to strip_prefix)."""
    if summary.startswith(PROGRESS_PREFIX) and PROGRESS_SEP in summary:
        return summary.split(PROGRESS_SEP, 1)[1]
    return summary


def base_title(summary: str) -> str:
    return strip_prefix(strip_progress(summary or ""))


DESC_GEAR = re.compile(r"^\s*\[([^\]]{0,40})\]")


def normalise_gear_text(text: str) -> str:
    """Fold the spellings voice produces onto the picker's own words. Spelling only."""
    t = (text or "").lower()
    t = re.sub(r"\b(?:extra|x)[\s-]*high\b", "xhigh", t)
    t = re.sub(r"\bopus[\s-]*1\s*m\b", "opus-1m", t)
    t = re.sub(r"\bmaximum\b", "max", t)
    return t


def detect_gear(summary: str, cfg: dict, description: str = "") -> tuple[str, str]:
    """The resolved gear the voice side asked for: (model, effort). A leading "[opus xhigh]" in
    the description wins (per-turn: a whiteboard turn edits only the description), else the title.
    Either half may be omitted and falls back to the configured default; a bracket carrying no
    gear at all (an ordinary "[…]" aside) defers to the title. Never inferred from the topic."""
    m = DESC_GEAR.match(description or "")
    for source in ([m.group(1)] if m else []) + [base_title(summary)]:
        words = re.findall(r"[a-z0-9-]+", normalise_gear_text(source))
        model = next((w for w in words if w in MODELS), None)
        effort = next((w for w in words if w in EFFORTS), None)
        if model or effort:
            return model or cfg["default_model"], effort or cfg["default_effort"]
    return cfg["default_model"], cfg["default_effort"]


def thread_root(state: dict, event_id: str) -> str:
    """Follow reply_to links ('Re:' events) back to the thread's first event."""
    seen = set()
    cur = event_id
    while cur not in seen:
        seen.add(cur)
        parent = state["processed"].get(cur, {}).get("reply_to")
        if not parent:
            return cur
        cur = parent
    return cur


def thread_path(cfg: dict, root: str) -> Path:
    return Path(cfg["spool_dir"]) / "threads" / f"{root}.md"


def thread_append(cfg: dict, root: str, title: str, heading: str, text: str) -> None:
    p = thread_path(cfg, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(f"# Thread {root} — {title}\n")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"\n## {heading}\n\n{text.strip()}\n")


def find_reply_target(state: dict, summary: str) -> str | None:
    """'Re: <title>' → the processed event id whose stripped title matches."""
    if not summary.lower().startswith("re:"):
        return None
    wanted = summary[3:].strip().lower()
    best = None
    for eid, rec in state["processed"].items():
        if rec.get("summary", "").strip().lower() == wanted:
            if best is None or rec.get("claimed_at", "") > state["processed"][best].get("claimed_at", ""):
                best = eid
    return best


# ----------------------------------------------------------------------------- sync
def pull_changes(svc, cfg: dict, state: dict) -> list[dict]:
    """Incremental list via syncToken; first run bootstraps a token."""
    from googleapiclient.errors import HttpError

    cal = cfg["calendar_id"]
    events: list[dict] = []
    page = None
    params = {"calendarId": cal, "singleEvents": True, "maxResults": 250, "showDeleted": True}
    if state.get("sync_token"):
        params["syncToken"] = state["sync_token"]
    else:
        # Bootstrap window: from the last successful poll (minus a day of slack, so a
        # past-dated event created since then still lists), else backfill_hours.
        if state.get("last_poll_at"):
            since = datetime.fromisoformat(state["last_poll_at"]) - timedelta(days=1)
        else:
            since = datetime.now(timezone.utc) - timedelta(hours=float(cfg["backfill_hours"]))
        params["timeMin"] = since.isoformat()
        log(f"no sync token — bootstrapping from {since.isoformat(timespec='minutes')}")
    while True:
        try:
            resp = svc.events().list(pageToken=page, **params).execute()
        except HttpError as e:
            if e.resp.status == 410:  # token expired → full resync
                log("sync token expired (410) — resetting")
                state["sync_token"] = None
                save_state(cfg, state)
                return pull_changes(svc, cfg, state)
            raise
        events.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            state["sync_token"] = resp.get("nextSyncToken")
            state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
            break
    return events


def claim(svc, cfg: dict, state: dict, ev: dict, *, dry_run: bool = False,
          undelivered_reply: str | None = None) -> None:
    """Claim a request — a brand-new event, or a NEW TURN on an already-answered one
    (whiteboard). Writes the inbox file, appends the question to the thread file."""
    eid = ev["id"]
    summary = base_title(ev.get("summary") or "(untitled)")
    rec = state["processed"].get(eid)
    turn = (rec.get("turn", 1) + 1) if rec else 1
    reply_to = rec.get("reply_to") if rec else find_reply_target(state, summary)
    description = (ev.get("description") or "").strip()
    model, effort = detect_gear(summary, cfg, description)
    if dry_run:
        what = f"new turn {turn} on" if rec else "claim"
        log(f"[dry-run] would {what} {eid}: {summary!r} gear={model}/{effort}" + (f" (re: {reply_to})" if reply_to else ""))
        return
    claimed_at = now_iso(cfg["timezone"])
    body = {
        "summary": PREFIX["claimed"] + summary,
        "colorId": COLOR["claimed"],
        "extendedProperties": {"private": {
            "comms_state": "claimed",
            "comms_claimed_at": claimed_at,
            "comms_stream": cfg["stream"],
            "comms_turn": str(turn),
            "comms_contract": str(CONTRACT),
        }},
    }
    off = int(cfg.get("traffic_offset_years") or 0)
    if off and turn == 1:
        # live traffic lives in the stream's traffic band: same date and time, off years forward
        st, en = ev.get("start", {}), ev.get("end", {})
        def _shift(x):
            return str(int(x[:4]) + off) + x[4:]
        if "dateTime" in st and int(st["dateTime"][:4]) < 2900:
            body["start"] = {**st, "dateTime": _shift(st["dateTime"])}
            body["end"] = {**en, "dateTime": _shift(en["dateTime"])}
        elif "date" in st and int(st["date"][:4]) < 2900:
            body["start"] = {"date": _shift(st["date"])}
            body["end"] = {"date": _shift(en["date"])}
        body["reminders"] = {"useDefault": False}
    svc.events().patch(calendarId=cfg["calendar_id"], eventId=eid, body=body).execute()
    if rec is None:
        state["processed"][eid] = {"summary": summary, "reply_to": reply_to}
    rec = state["processed"][eid]
    rec.update({"claimed_at": claimed_at, "state": "claimed", "turn": turn,
                "original_description": description, "last_written": None})
    root = thread_root(state, eid)
    thread_append(cfg, root, summary, f"Turn {turn} — {cfg['stream']} asked · {claimed_at[:16]}", description or "(no body)")
    if undelivered_reply:
        thread_append(cfg, root, summary, f"Turn {turn - 1} — reply NOT delivered (slot was overwritten) · {claimed_at[:16]}",
                      undelivered_reply)
    req = {
        "contract": CONTRACT,
        "event_id": eid,
        "calendar_id": cfg["calendar_id"],
        "stream": cfg["stream"],
        "kind": "followup" if reply_to else ("turn" if turn > 1 else "request"),
        "reply_to": reply_to,
        "turn": turn,
        "model": model,
        "effort": effort,
        "thread_file": str(thread_path(cfg, root)),
        "thread_root": root,
        "summary": summary,
        "description": description,
        "undelivered_reply": undelivered_reply,
        "start": ev.get("start", {}),
        "created": ev.get("created"),
        "updated": ev.get("updated"),
        "html_link": ev.get("htmlLink"),
        "claimed_at": claimed_at,
    }
    inbox = Path(cfg["spool_dir"]) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = inbox / f".{eid}.json.tmp"
    tmp.write_text(json.dumps(req, indent=1, ensure_ascii=False))
    tmp.replace(inbox / f"{eid}.json")  # atomic appear
    log(f"claimed {eid} turn {turn} gear {model}/{effort}: {summary!r}" + (f" (re: {reply_to})" if reply_to else ""))


def is_new_turn(state: dict, ev: dict) -> bool:
    """An answered event whose (non-empty) description differs from what we last wrote."""
    rec = state["processed"].get(ev["id"])
    if not rec or rec.get("state") not in ("done", "question"):
        return False
    desc = (ev.get("description") or "").strip()
    if not desc:
        return False  # the wipe half of a wipe-then-write edit: wait for the write
    if "turn" not in rec:
        # Answered before the whiteboard (reply appended under "Reply from <stream> · …"):
        # only a description that no longer carries that reply is a new turn.
        return f" from {rec.get('stream', '')}".strip() not in desc and "Reply from" not in desc and "Question from" not in desc
    return desc != (rec.get("last_written") or "").strip()


def apply_reply(svc, cfg: dict, state: dict, event_id: str, status: str, text: str) -> None:
    if status not in ("done", "question", "progress"):
        raise ValueError("status must be done|question|progress")
    from googleapiclient.errors import HttpError

    cal = cfg["calendar_id"]
    ev = svc.events().get(calendarId=cal, eventId=event_id).execute()
    base = base_title(ev.get("summary") or "")
    rec = state["processed"].setdefault(event_id, {"summary": base})
    root = thread_root(state, event_id)
    turn = rec.get("turn", 1)
    stamp = now_iso(cfg["timezone"])
    if status == "progress":
        # Heartbeat: TITLE only — the description is the user's slot and stays untouched.
        body = {"summary": PROGRESS_PREFIX + text.strip().replace("\n", " ")[:80] + PROGRESS_SEP + base}
        svc.events().patch(calendarId=cal, eventId=event_id, body=body).execute()
        thread_append(cfg, root, base, f"Turn {turn} — progress · {stamp[:16]}", text)
        log(f"progress on {event_id}: {text.strip()[:60]!r}")
        return
    # Guard: the phone may have written the next turn while this reply waited in the outbox.
    current = (ev.get("description") or "").strip()
    if rec.get("state") == "claimed" and current and current != (rec.get("original_description") or "").strip():
        log(f"slot on {event_id} changed while we worked — claiming the new turn, reply attached")
        claim(svc, cfg, state, ev, undelivered_reply=text)
        return
    reply = text.strip()
    body = {
        "summary": PREFIX[status] + base,
        "colorId": COLOR[status],
        "description": reply,
        "extendedProperties": {"private": {"comms_state": status, "comms_replied_at": stamp,
                                           "comms_turn": str(turn)}},
    }
    check_cap(body.get("description") or "", f"reply on {event_id}")
    if cfg["reply_buzz"]:
        # Reminders only fire ahead of the start, so slide the event to now + popup + lead:
        # the popup fires in `lead` minutes and the phone shows the title (now carrying ✓ or ?)
        # as a `popup`-minute reminder, which lingers instead of auto-dismissing.
        tz = ZoneInfo(cfg["timezone"])
        popup = int(cfg["reply_buzz_popup_minutes"])
        start = datetime.now(tz) + timedelta(minutes=popup + int(cfg["reply_buzz_lead_minutes"]))
        end = start + timedelta(minutes=15)
        body["start"] = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": cfg["timezone"], "date": None}
        body["end"] = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": cfg["timezone"], "date": None}
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": popup}]}
    req = svc.events().patch(calendarId=cal, eventId=event_id, body=body)
    req.headers["If-Match"] = ev["etag"]  # conditional write: never clobber a newer turn
    try:
        req.execute()
    except HttpError as e:
        if e.resp.status != 412:
            raise
        log(f"etag conflict on {event_id} — re-reading")
        fresh = svc.events().get(calendarId=cal, eventId=event_id).execute()
        newest = (fresh.get("description") or "").strip()
        if newest and newest != (rec.get("original_description") or "").strip():
            claim(svc, cfg, state, fresh, undelivered_reply=text)
            return
        req = svc.events().patch(calendarId=cal, eventId=event_id, body=body)
        req.headers["If-Match"] = fresh["etag"]
        req.execute()
    rec["state"] = status
    rec["last_written"] = reply
    thread_append(cfg, root, base, f"Turn {turn} — {cfg['stream']} replied ({status}) · {stamp[:16]}", reply)
    log(f"replied {status} on {event_id} turn {turn}: {base!r}")


def push_outbox(svc, cfg: dict, state: dict) -> int:
    outbox = Path(cfg["spool_dir"]) / "outbox"
    done_dir = outbox / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(outbox.glob("*.json")):
        try:
            msg = json.loads(f.read_text())
            apply_reply(svc, cfg, state, msg["event_id"], msg.get("status", "done"), msg.get("text", ""))
            f.replace(done_dir / f.name)
            n += 1
        except Exception as e:  # keep the file for the next pass; never lose a reply
            log(f"outbox {f.name} failed: {e}")
    return n


def poll_once(svc, cfg: dict, state: dict, *, dry_run: bool = False) -> None:
    saved_token = state.get("sync_token")
    changed = pull_changes(svc, cfg, state)
    for ev in changed:
        if ev.get("status") == "cancelled":
            continue
        if is_note(ev, cfg):
            # passive context note: never claimed. A PUBLISHED note edited on the calendar is the down-pipe's
            # business (ticks only, tools/down_pipe.py; off unless down_pipe is set in comms.toml).
            if not dry_run and (cfg.get("down_pipe") or "off") != "off" and \
                    (ev.get("extendedProperties") or {}).get("private", {}).get("publish_path"):
                sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
                import down_pipe  # noqa: PLC0415  (lazy: tools/down_pipe imports this module)
                down_pipe.on_event(svc, cfg, ev)
            continue
        if ev["id"] in state["processed"]:
            if is_new_turn(state, ev):
                claim(svc, cfg, state, ev, dry_run=dry_run)  # whiteboard: next turn of the thread
            continue
        if comms_state(ev):
            continue  # claimed/answered by an earlier state file we no longer have: leave it
        if (ev.get("summary") or "").lstrip().startswith(PREFIX["claimed"].strip()):
            continue  # carries our claimed mark but no state: not ours to touch
        # Any other prefix on a stateless event (a leading "?" or tick typed by the voice side) is just
        # the human's own punctuation: claim it; base_title strips it. (2026-09-14: six requests sat
        # unseen because the voice side began titling questions "? …".)
        claim(svc, cfg, state, ev, dry_run=dry_run)
    if dry_run:
        state["sync_token"] = saved_token  # rehearsal: the next real poll sees the same changes
        log("[dry-run] nothing written; state not saved")
        return
    save_state(cfg, state)
    if push_outbox(svc, cfg, state):
        save_state(cfg, state)


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).parent / "comms.toml"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--auth", action="store_true")
    g.add_argument("--list-calendars", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--reply", metavar="EVENT_ID")
    g.add_argument("--show", metavar="EVENT_ID")
    ap.add_argument("--status", choices=["done", "question", "progress"], default="done")
    ap.add_argument("--dry-run", action="store_true", help="with --once: rehearse, write nothing")
    ap.add_argument("--text", default="")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    if args.auth:
        return do_auth(cfg)
    svc = get_service(cfg)
    if args.list_calendars:
        for c in svc.calendarList().list().execute().get("items", []):
            print(f"{c['id']:60s}  {c.get('summary')}  {'(primary)' if c.get('primary') else ''}")
        return
    if "calendar_id" not in cfg:
        sys.exit("comms.toml needs calendar_id (see --list-calendars)")
    state = load_state(cfg)
    if args.show:
        print(json.dumps(svc.events().get(calendarId=cfg["calendar_id"], eventId=args.show).execute(), indent=1))
    elif args.reply:
        apply_reply(svc, cfg, state, args.reply, args.status, args.text)
        save_state(cfg, state)
    elif args.once:
        poll_once(svc, cfg, state, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
