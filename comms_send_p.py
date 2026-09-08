#!/usr/bin/env python3
"""calendar-communication fallback sender (route 3): deliver spool/inbox requests to the named inbox
session with a one-shot `claude -p` that calls the documented SendMessage tool.

Costs one small model call per request (~10 s); needs no preview flags.  The receiving
session has no comms_reply tool on this route — it replies with bin/comms-reply instead.

  comms_send_p.py --config comms.toml            # deliver everything in the inbox
  comms_send_p.py --config comms.toml --dry-run  # show the prompt, send nothing
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).parent / "comms.toml"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = tomllib.load(open(args.config, "rb"))
    base = Path(args.config).parent
    spool = (base / cfg.get("spool_dir", "spool")).expanduser()
    session = cfg.get("session_name", "comms inbox")
    reply_cmd = str((base / "bin" / "comms-reply").resolve())
    inbox, delivered = spool / "inbox", spool / "inbox" / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    claude = shutil.which("claude") or "/run/current-system/sw/bin/claude"

    for f in sorted(inbox.glob("*.json")):
        req = json.loads(f.read_text())
        body = [
            f"Comms {req.get('kind', 'request')} from the {req.get('stream')} calendar (event_id: {req['event_id']})",
            f"Title: {req['summary']}",
        ]
        if req.get("reply_to"):
            body.append(f"Follow-up to event_id: {req['reply_to']}")
        if req.get("description", "").strip():
            body += ["", req["description"].strip()]
        body += ["", "When finished, reply from your shell with:",
                 f"  {reply_cmd} {req['event_id']} done \"<short spoken-style brief>\"",
                 f"  {reply_cmd} {req['event_id']} question \"<one clear question>\""]
        message = "\n".join(body)
        prompt = (f"Use ListAgents to find the session named {session!r}, then SendMessage it the text between "
                  f"the markers, verbatim, and nothing else. Do no other work.\n<<<\n{message}\n>>>")
        if args.dry_run:
            print(prompt, "\n---")
            continue
        r = subprocess.run(
            [claude, "-p", "--permission-mode", "default", "--allowedTools", "ListAgents,SendMessage",
             "--output-format", "text", prompt],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode == 0:
            f.replace(delivered / f.name)
            print(f"delivered {f.name}: {r.stdout.strip()[:120]}")
        else:
            print(f"FAILED {f.name}: {r.stderr.strip()[:300]}", file=sys.stderr)


if __name__ == "__main__":
    main()
