# calendar-communication

Talk to a Claude Code session on your own machine from the Claude app's voice mode, using a
Google Calendar as the message bus. You speak; the phone writes a calendar event; a poller on
the machine hands it to a long-running, named Claude Code session that you can also open from
the phone via Remote Control; the reply lands back on the same event, the phone buzzes, and
voice reads it out.

```
Claude app (voice) ──creates event──▶  "<Stream>" calendar  ◀──⏳ ✓ ? + reply──  comms_poller.py  (every minute)
                                                                                       │ spool/inbox    ▲ spool/outbox
                                                                                       ▼                │
Claude app (Remote Control) ───────────────▶  "<Stream> inbox" Claude Code session ◀── comms_channel.mjs (channel shim)
```

One instance per machine or container, each with its own stream calendar, Google token,
session, spool and units. The code is stream-agnostic: every identity lives in `comms.toml`
and on the calendar itself.

## Read this first: what it rests on

- **Claude Code channels are a research preview.** The shim is loaded with
  `--dangerously-load-development-channels`, whose consent dialog appears at every start and
  needs a TTY. The launcher runs the session inside tmux and answers it. If Anthropic changes
  the flag or the dialog, the launcher is the file to fix.
- **Gear switching drives the `/model` picker with keystrokes** in that tmux pane. It parses
  and verifies, never types blind, but it is tied to the picker's layout. It ships off; run
  `bin/gear-switch-test` on each machine before turning it on, and again after Claude Code
  upgrades. Keep both instances on the same Claude Code version.
- **Voice mode can create and read calendar events** through the Google Calendar connector,
  but it acts only when you ask. It will not interrupt a conversation with a reply; the
  calendar popup and the Claude Code mobile push are the nudges, then you say "read the relay".
- Built and verified on Claude Code 2.1.259, Linux, a personal Gmail. Nothing here is
  endorsed by Anthropic or Google.

## Pieces

| file | side | role |
|---|---|---|
| `comms_poller.py` | Google | sync-token poll; claims new events (⏳) and new whiteboard turns; writes `spool/inbox/*.json`; pushes `spool/outbox/*.json` replies (✓ / ? replace the description; `progress` marks the title only); etag-conditional writes, a conflicting phone edit becomes the next turn with the undelivered reply attached; thread transcripts in `spool/threads/` |
| `comms_channel.mjs` | Claude | the channel shim and sequencer: admits ONE turn at a time into the session (done/question ends it, progress does not, 45-minute timeout), orders the queue by gear, optionally switches gear first, exposes the `comms_reply` tool |
| `gear_switch.mjs` | Claude | parse-and-verify `/model` picker driver (session-only confirm); `bin/gear-switch-test` rehearses it on a scratch session |
| `bin/inbox-session` | Claude | starts `claude --remote-control --name "<Stream> inbox"` in tmux with the shim, answers the consent, verifies registration, self-heals under systemd |
| `bin/comms-reply` | Claude | write an outbox reply by hand (testing) |
| `bin/comms-units` | ops | install / status / remove the two user units |
| `bin/comms-update` | ops | pull main, refresh deps if their manifests changed, smoke-test, reinstall units, restart the session if needed |
| `bin/comms-push` | ops | publish local commits: rebase onto `origin/main`, then push — refuses a dirty tree, never forces |
| `tools/down_pipe.py` | Google | calendar -> vault, ticks only: box ticked, number over a blank, a line under Notes; log or apply per instance (`down_pipe`), scoped by layer year, snapshot + journal before writes |
| `tools/note_protocol.py` | Google | publish this stream's copy of the phone rules as a context note and rebuild `Note: Index` |
| `tools/wakeup.py` | Google | the daily wake-up ritual: roll outstanding items to tomorrow, lay the welcome note on today, archive finished traffic into the band, rescue strays from the anchor date, shelve the meridian spans where `tcm-clock` exists (`deploy/comms-wakeup.timer`, 00:05 and 06:30 local, retried) |
| `tools/journal_export.py` | Google | one-way export of Note: events and finished threads into an Obsidian vault (`journal_dir` in comms.toml, opt-in): relay plumbing stripped, stable frontmatter ids, locally edited files left alone (`deploy/comms-journal.timer`, every 15 minutes) |
| `tools/meridian_day.py` | Google | ambient span writer for the TCM meridian clock (optional; needs `tcm-clock`) |
| `tools/keys.py` | — | per-instance inert search keys (P2) in `state/keys.json`, minted with a collision check |
| `docs/voice-profile.md` | phone | the phone half of the contract: one profile instruction for all streams |
| `deploy/` | ops | `comms-poller.timer` (every minute) and `comms-inbox.service` (Restart=always) |
| `comms.toml` | config | this instance's identity and settings (`comms.toml.example` is the template) |

## Contracts (version 1)

These are what the phone instruction, the poller and the shim all rely on. Change them
deliberately, bump `CONTRACT` in both `comms_poller.py` and `comms_channel.mjs`, and
republish the notes.

**Title grammar.** A new event has no prefix. `⏳ ` = claimed (yellow), `✓ ` = done (green),
`? ` = question (red), `⏳ <words> · ` = progress heartbeat. A trailing `(model effort)` on the
title, or a leading `[model effort]` on a turn's description, is the resolved gear. Titles
starting `Note:` (or events on `note_anchor_date`) are context notes and are never touched.

**Whiteboard turns.** An event is a slot holding the current turn. The phone asks the next
question by replacing the description in one edit; the reply replaces it again. The full
transcript lives in `spool/threads/<root>.md` and is restated in each reply.

**Spool.** `spool/inbox/<event_id>.json` (poller → shim) carries `contract, event_id, stream,
kind (request|followup|turn), turn, model, effort, summary, description, thread_file,
reply_to, undelivered_reply, claimed_at`. `spool/outbox/<event_id>.json` (shim → poller)
carries `contract, event_id, status (done|question|progress), text`. Files move to
`inbox/delivered/` and `outbox/done/` once consumed, so a restart on either side loses nothing.

**Calendar state.** Private extended properties `comms_state`, `comms_turn`,
`comms_contract`, `comms_claimed_at`, `comms_replied_at` (invisible on the phone).

## Setup

1. **Google Cloud** (once per Google account, in a browser): create a project, enable the
   *Google Calendar API*, set up the OAuth consent screen (External, yourself as a test user),
   then set publishing status to **In production** so refresh tokens stop expiring after 7
   days. Under *Google Auth Platform → Clients* create a **Desktop app** client and download
   its JSON.
2. **Calendar**: in Google Calendar, *Other calendars → + → Create new calendar*, named after
   the stream. Do not share it.
3. **This machine** (needs Python 3.11+, Node 18+, tmux, curl, a logged-in `claude` with
   Remote Control consent accepted once, and `loginctl enable-linger` for user services):
   ```bash
   git clone git@github.com:<you>/calendar-communication.git ~/calendar-communication
   cd ~/calendar-communication
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && npm ci
   cp comms.toml.example comms.toml            # stream, session_name, session_cwd, timezone
   mkdir -p state && cp ~/Downloads/client_secret_*.json state/client_secret.json
   .venv/bin/python comms_poller.py --auth       # prints a URL; paste the failed localhost redirect back
   .venv/bin/python comms_poller.py --list-calendars   # → calendar_id into comms.toml
   .venv/bin/python comms_poller.py --once
   node test/channel_smoke.mjs
   bin/comms-units install
   .venv/bin/python tools/note_protocol.py --apply     # publish the rules note + index on this calendar
   ```
4. **Phone**: paste the block from `docs/voice-profile.md` into the Claude app's profile
   instructions, listing your streams on its first line. In the inbox session, `/config` →
   turn on *Push when Claude decides* and *Push when actions required* if you want Claude app
   pushes on top of the calendar popup.

## Operating it

- **Watch**: `tmux attach -t comms-inbox` (detach with `Ctrl-b d`). `bin/comms-units status`.
  Poller log: `journalctl --user -u comms-poller.service`. Session debug log:
  `state/inbox-debug.log` (previous start in `.1`).
- **Update an instance**: `bin/comms-update` (add `--restart` to bounce the session when the
  shim or launcher changed). Every other instance pulls what one instance pushed.
- **Develop on any instance**, but there is ONE `main` and no per-instance branches: commit
  small, smoke-test and do a live round trip, then `bin/comms-push` straight away (it rebases
  onto `origin/main` before pushing and never forces). `comms.toml`, `state/` and `spool/` are
  git-ignored, so instance identity can never conflict. Each instance's deploy key needs
  **write access** for this — on GitHub that is the *Allow write access* box when the key is
  added; a read-only key cannot be upgraded, delete it and re-add the same public key.
- **Rehearse gear switching** before enabling it: `bin/gear-switch-test opus high`.
- **A stuck turn** clears itself after `turn_timeout_minutes`; restarting the inbox service
  also ends the in-flight turn (by construction, nothing can wedge).

## Test ladder for a new instance

1. Phone: voice-create an event on the stream calendar; voice-read its description back.
2. `comms_poller.py --once` claims it (⏳ on the phone).
3. `node test/channel_smoke.mjs` drives the shim over stdio without Claude.
4. Units installed: the session answers, the poller pushes ✓, the phone buzzes, voice reads it.

## Known limits

- Google sync tokens sometimes expire early; the poller re-syncs from the last successful
  poll minus a day, and deduplicates by event id.
- The description field is capped by Google at roughly 8 KB; long output goes into a
  `Note:` event and the reply carries its title.
- Haiku has no auto mode; a gear naming it is refused (the session would drop to manual).
- One stream per instance. Two streams on one machine means two checkouts.
