# nexus-comms

Phone (Claude app, voice) → Google Calendar → this container's **inbox session** (a named
Claude Code session you can also open from the phone via Remote Control) → reply back onto
the calendar event → voice reads it. One copy per container, one stream calendar each.

```
phone voice ──creates event──▶ "Woolly" calendar ◀──patches ⏳ ✓ ? + brief── comms_poller.py
                                                                             │ spool/inbox   ▲ spool/outbox
                                                                             ▼               │
                                                     inbox session ◀── comms_channel.mjs (route 2)
                                                     "Woolly inbox"     or comms_send_p.py  (route 3)
                                                          ▲
phone Remote Control ─────────────────────────────────────┘
```

## Pieces

| file | side | role |
|---|---|---|
| `comms_poller.py` | Google | sync-token poll, claim new events (⏳), write `spool/inbox/*.json`, push `spool/outbox/*.json` replies (✓ / ?, brief, buzz) |
| `comms_channel.mjs` | Claude | **route 2**: MCP channel server run by the session; pushes inbox files in as `<channel>` events, exposes `comms_reply` |
| `comms_send_p.py` | Claude | **route 3** fallback: `claude -p` + SendMessage per inbox file |
| `bin/comms-reply` | Claude | write an outbox reply from a shell (route 3 or by hand) |
| `bin/inbox-session` | Claude | launches `claude --remote-control --name "<Stream> inbox"` with the route-2 flags |
| `deploy/*.service,*.timer` | both | user-level systemd units (poller minutely; inbox session Restart=always) |
| `comms.toml` | config | calendar id, stream, session name/cwd/mode, route flags |

The spool directory is the only contract between the two sides, so either side can be
swapped or restarted without losing a request: inbox files move to `inbox/delivered/`
once pushed into the session, outbox files to `outbox/done/` once on the calendar.

## Event lifecycle (title prefix = state, private extended properties = machine state)

| state | title | colour | who | what |
|---|---|---|---|---|
| request | `Check SY stock for Hamelton` | – | phone | any new event on the stream calendar |
| claimed | `⏳ Check SY…` | yellow | poller ≤60 s | inbox file written; visible "picked up" on the phone |
| done | `✓ Check SY…` | green | session → poller | brief appended under `———`; event slid to now+2 min with a 1-min popup so the phone buzzes |
| question | `? Check SY…` | red | session → poller | same, text is the question |
| follow-up | `Re: Check SY…` | – | phone | new request carrying `reply_to` = the matching earlier event |

## Setup (per container)

1. **Google Cloud** (once for the account, in a browser): create a project, enable the
   *Google Calendar API*, configure the OAuth consent screen as *External*, add the Gmail as
   a test user, then set publishing status to **In production** (unverified is fine for
   personal use; it stops the 7-day refresh-token expiry). Create an OAuth client of type
   *Desktop app* and download its JSON.
2. **Calendar**: in Google Calendar, *Other calendars → Create new calendar* named after the
   stream (e.g. `Woolly`). Don't share it.
3. **This box**:
   ```bash
   cp comms.toml.example comms.toml           # edit stream / session_name / session_cwd
   mkdir -p state && cp ~/Downloads/client_secret_*.json state/client_secret.json
   ../woolly-workplace/.venv/bin/python comms_poller.py --auth      # paste-the-URL consent
   ../woolly-workplace/.venv/bin/python comms_poller.py --list-calendars   # → calendar_id
   ../woolly-workplace/.venv/bin/python comms_poller.py --once
   ```
   (python needs `google-api-python-client google-auth-oauthlib`; the woolly venv has them.)
4. **Route 2 (channel shim)**: nothing to register — `bin/inbox-session` writes the MCP
   config for the shim to `state/mcp-config.json` and passes it with `--mcp-config`. It runs
   the session inside tmux session `comms-inbox` and answers the development-channels
   consent dialog itself (that dialog appears at *every* start and is not persisted, which
   is also why route 2 cannot run under `claude -p`). `tmux attach -t comms-inbox` to watch.
   **Route 3** instead: set `channel_flags = ""` in `comms.toml` and uncomment the
   `ExecStartPost` line in the poller service.
5. **Units**: `cp deploy/* ~/.config/systemd/user/ && systemctl --user daemon-reload &&
   systemctl --user enable --now nexus-comms-poller.timer nexus-comms-inbox.service`.
6. **Phone**: in the inbox session run `/config` and turn on *Push when Claude decides* and
   *Push when actions required* (permission prompts then reach the phone). In the Claude
   app, Settings → Profile → *Instructions for Claude*, paste something like:

   > I have Google calendars named "Woolly" and "Frames" that comms requests to agents.
   > When I ask you to send/hand something to Woolly or Frames, create a 15-minute event
   > starting now on that calendar: title = short imperative summary, description = my full
   > request. No attendees or reminders. When I ask what came back, or say "check the
   > comms", look at those calendars for events in the last 24 h whose title starts with
   > ✓ or ? and read me the text after the ——— line. If it starts with ?, the agent needs
   > my answer: send it as a new event titled "Re: <original title>".

## Test ladder

0. Phone, no code: voice-create an event on the stream calendar; voice-read the latest
   event's description. Does voice mode write, and are profile instructions honoured?
1. `comms_poller.py --once` claims a hand-made event (⏳ appears on the phone).
2. Channel shim alone: `node test/channel_smoke.mjs` (drives it over stdio, no Claude).
3. Inbox session in the foreground with the channel flag: drop a file in `spool/inbox/`,
   watch the `<channel>` event land, see `comms_reply` write the outbox, poller pushes ✓,
   phone buzzes, voice reads it.
4. Units enabled; repeat from the phone end to end. Then the second container.

## Verified so far (2026-09-08, this box, Claude Code 2.1.259)
- poller state machine offline against a fake calendar: claim → idempotent → reply/buzz → follow-up linking
- `node test/channel_smoke.mjs`: shim capability, tool list, notification in ~300 ms, outbox write
- **route 2 end to end in a real interactive session**: `Channel notifications registered`,
  the spool file arrived as `← comms: Comms request…`, Claude called `comms_reply`, outbox
  written (17 × 23 → 391). Headless `-p` runs never register the channel: the consent
  dialog needs a TTY.
- not yet: anything touching Google (no OAuth client exists yet), the units, the phone.

## Not decided yet (deliberately)
route 2 vs 3 · package home (this dir vs a repo) · session permission mode · push toggles ·
stream names. Everything above runs either way.
