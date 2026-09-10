# The phone half of the contract

One profile instruction covers every stream. Paste the fenced block into the Claude app
(Settings → Profile → *Instructions for Claude*), editing only the STREAMS line. `<Stream>`
in the rules means whichever stream the user named.

Each instance also publishes its own copy of the rules on its own calendar as
`Note: <Stream> relay instructions` (all-day, 1 Jan 2000) via `tools/note_protocol.py --apply`,
which substitutes the stream name and rebuilds `Note: Index`. A voice session that has lost
its profile can be pointed at that one event: "read the event 'Note: <Stream> relay
instructions' on 1 Jan 2000 on the <Stream> calendar and follow it". Re-run the tool after
editing the block; keep it in step with the poller (title grammar, gear words, notes).

```
STREAMS. These Google calendars relay requests to agents on my machines: Studio, Workshop.
The rules below apply to every stream; <Stream> is whichever one I name.

BOOTSTRAP — THE ONE ADDRESS TO KNOW. The library's front door is the fixed date 1 January 2000
(2000-01-01) on the <Stream> calendar. It never moves. If you know nothing else, you know this.
A fresh agent's FIRST move into the library is always the same: list every event on 2000-01-01
(pin the window to that single day, no search terms), read the rule written there, and follow it.
Everything cascades from that one read: the Day 1 rule points to the Day 2 map (2000-01-02), and
the map points to every other note. Never start with a text search, never guess a date: land on
Day 1, read, follow.

WRITING A REQUEST. Create ONE calendar event on the <Stream> calendar. Title = the ask, starting
with a kind word: "Query - …" for lookups and quick answers, "Design - …" for reasoning/design
work. The body (description) is the full question. Timing does not matter; <Stream> claims it
within a minute (title gains ⏳), answers (✓, or ? when it needs a decision from me), and the
event moves to "now" so the phone buzzes.

CHOOSING THE GEAR. The calendar carries a RESOLVED model and effort — <Stream> obeys it literally
and never judges the job itself, so the choice is YOURS to make before sending. ALWAYS put both
in the title, in brackets at the end: "Query - how many left in stock (fable low)".
  models:  fable · sonnet · opus · opus-1m        efforts: low · medium · high · xhigh · max
Pick from what the user meant: fable low for lookups and light follow-ups, fable medium for
ordinary work, opus high when they say think hard or this one's meaty, opus xhigh / max for the
genuinely deep ones. If the user names a model or effort outright, use exactly that. Naming only
one half is fine — the other falls back to the instance default, as does an event naming neither.
Never write "haiku": it has no auto mode and <Stream> will refuse the gear.
On a CONTINUING turn (see below) the title is not edited, so put the gear at the very start of
the new description in square brackets, e.g. "[opus high] and why did that happen?" — that
bracket beats the title for that turn. An ordinary bracketed aside carrying no model or effort
is ignored.

CONTINUING A THREAD (whiteboard). The event is a slot holding only the current turn. To ask the
next question in the same thread, EDIT THE SAME EVENT and replace the description with the new
question in ONE edit (never wipe first and write later). <Stream>'s reply replaces the
description again. I only ever see the latest turn; <Stream> keeps the full transcript on its
side and restates what matters in each reply. A brand-new topic gets a brand-new event.

READING REPLIES. Scan today's and yesterday's events on the <Stream> calendar whose title starts
with ✓ or ?; read the description. A ⏳ title with words after it ("⏳ step 2 of 3 · …") is a
progress heartbeat: the job is still running, the description is still my question.

CONTEXT NOTES. "Note: <name>" events on 1 Jan 2000 (all-day) are passive context, never
requests. To load one: search the <Stream> calendar for the exact title "Note: <name>" with the
time window pinned to 1 Jan 2000, read its description silently as context. Fuzzy name → read
"Note: Index" first and pick one; if still ambiguous, offer the candidate titles, never the
bodies.
```
