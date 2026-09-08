#!/usr/bin/env python3
"""calendar-communication poller — the Google Calendar side of the comms.

One stream calendar (a secondary calendar on the Gmail account) is the message bus:

  request   : an event the phone created (no state yet, no title prefix)
  claimed   : we saw it → "⏳ " prefix, yellow, inbox file written for the session
  done      : session replied → "✓ " prefix, green, brief appended to the description,
              event moved to "now" with a 1-minute popup reminder so the phone buzzes
  question  : session needs input → "? " prefix, red, same treatment

State lives in the event's private extended properties (invisible on the phone) plus a
small local state file (sync token + processed ids).  The session side talks to us only
through the spool directory:

  spool/inbox/<event_id>.json    request for the session (channel shim / -p sender picks up)
  spool/outbox/<event_id>.json   reply from the session  {"event_id","status","text"}

Commands
  --auth            one-time OAuth consent (paste-the-redirect-URL flow, works headless)
  --list-calendars  show calendar ids (to fill comms.toml)
  --once            one poll: pull changes, claim new requests, push pending replies
  --loop            poll forever (interval from config)
  --reply ID --status done|question --text "…"   hand-written reply (testing)
  --show ID         dump one event
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
PREFIX = {"claimed": "⏳ ", "done": "✓ ", "question": "? "}
COLOR = {"claimed": "5", "done": "10", "question": "11"}  # Banana / Basil / Tomato
SEPARATOR = "\n\n———\n"


# ----------------------------------------------------------------------------- config
def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = path.parent
    cfg.setdefault("stream", "comms")
    cfg.setdefault("timezone", "Australia/Melbourne")
    cfg.setdefault("poll_interval_seconds", 60)
    cfg.setdefault("backfill_hours", 0)
    cfg.setdefault("reply_buzz", True)
    cfg.setdefault("reply_buzz_lead_minutes", 2)
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
            break
    return events


def claim(svc, cfg: dict, state: dict, ev: dict) -> None:
    eid = ev["id"]
    summary = ev.get("summary") or "(untitled)"
    reply_to = find_reply_target(state, summary)
    claimed_at = now_iso(cfg["timezone"])
    body = {
        "summary": PREFIX["claimed"] + summary,
        "colorId": COLOR["claimed"],
        "extendedProperties": {"private": {
            "comms_state": "claimed",
            "comms_claimed_at": claimed_at,
            "comms_stream": cfg["stream"],
        }},
    }
    svc.events().patch(calendarId=cfg["calendar_id"], eventId=eid, body=body).execute()
    req = {
        "event_id": eid,
        "calendar_id": cfg["calendar_id"],
        "stream": cfg["stream"],
        "kind": "followup" if reply_to else "request",
        "reply_to": reply_to,
        "summary": summary,
        "description": ev.get("description") or "",
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
    state["processed"][eid] = {"summary": summary, "claimed_at": claimed_at, "state": "claimed",
                               "original_description": ev.get("description") or ""}
    log(f"claimed {eid}: {summary!r}" + (f" (re: {reply_to})" if reply_to else ""))


def apply_reply(svc, cfg: dict, state: dict, event_id: str, status: str, text: str) -> None:
    if status not in ("done", "question"):
        raise ValueError("status must be done|question")
    cal = cfg["calendar_id"]
    ev = svc.events().get(calendarId=cal, eventId=event_id).execute()
    base_summary = strip_prefix(ev.get("summary") or "")
    rec = state["processed"].get(event_id, {})
    original = rec.get("original_description", ev.get("description") or "")
    stamp = datetime.now(ZoneInfo(cfg["timezone"])).strftime("%a %d %b %H:%M")
    label = "Reply" if status == "done" else "Question"
    description = (original.rstrip() + SEPARATOR if original.strip() else "") + f"{label} from {cfg['stream']} · {stamp}\n{text.strip()}"
    body = {
        "summary": PREFIX[status] + base_summary,
        "colorId": COLOR[status],
        "description": description,
        "extendedProperties": {"private": {"comms_state": status, "comms_replied_at": now_iso(cfg["timezone"])}},
    }
    if cfg["reply_buzz"]:
        # Reminders only fire ahead of the start, so slide the event to "now" and set a
        # 1-minute popup: the phone buzzes with the title (which now carries ✓ or ?).
        tz = ZoneInfo(cfg["timezone"])
        start = datetime.now(tz) + timedelta(minutes=int(cfg["reply_buzz_lead_minutes"]))
        end = start + timedelta(minutes=15)
        body["start"] = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": cfg["timezone"], "date": None}
        body["end"] = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": cfg["timezone"], "date": None}
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1}]}
    svc.events().patch(calendarId=cal, eventId=event_id, body=body).execute()
    state["processed"].setdefault(event_id, {"summary": base_summary})
    state["processed"][event_id]["state"] = status
    log(f"replied {status} on {event_id}: {base_summary!r}")


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


def poll_once(svc, cfg: dict, state: dict) -> None:
    changed = pull_changes(svc, cfg, state)
    for ev in changed:
        if ev.get("status") == "cancelled":
            continue
        if comms_state(ev) or ev["id"] in state["processed"]:
            continue
        if strip_prefix(ev.get("summary") or "") != (ev.get("summary") or ""):
            continue  # a prefixed title we somehow don't know: leave it alone
        claim(svc, cfg, state, ev)
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
    g.add_argument("--loop", action="store_true")
    g.add_argument("--reply", metavar="EVENT_ID")
    g.add_argument("--show", metavar="EVENT_ID")
    ap.add_argument("--status", choices=["done", "question"], default="done")
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
        poll_once(svc, cfg, state)
    elif args.loop:
        interval = int(cfg["poll_interval_seconds"])
        log(f"polling {cfg['calendar_id']} every {interval}s")
        while True:
            try:
                poll_once(svc, cfg, state)
            except Exception as e:
                log(f"poll failed: {e}")
            time.sleep(interval)


if __name__ == "__main__":
    main()
