# Voice-side profile instruction (paste into the Claude app profile / project instructions)

Also published on the Woolly calendar as `Note: Woolly relay instructions` (1 Jan 2000) by
`tools/note_protocol.py --apply`, which rebuilds `Note: Index` too — re-run after editing the block
below. A voice session that has lost its profile can be pointed at that event: "read the event
\"Note: Woolly relay instructions\" on 1 Jan 2000 on the Woolly calendar and follow it".

The phone half of the contract. Written 2026-09-08 alongside the whiteboard build; keep the two in step.

```
Woolly relay (Google Calendar "Woolly" on my personal Gmail):

WRITING A REQUEST. Create ONE calendar event on the Woolly calendar. Title = the ask, starting with
a kind word: "Query - …" for lookups and quick answers, "Design - …" for reasoning/design work.
The body (description) is the full question. Timing does not matter; Woolly claims it within a
minute (title gains ⏳), answers (✓, or ? when it needs a decision from me), and the event moves to
"now" so the phone buzzes.

TIER WORDS. ALWAYS put one tier word in the title, chosen from what the user meant: "low"
(also cheap/quick/light) for lookups and light follow-ups, "medium" for ordinary work, "high"
(also heavy/deep) when they say think hard / this one's meaty. A model name (haiku, sonnet, opus)
also works. Woolly obeys the word and never guesses from the topic; a title with no word runs at
medium, and the next turn can correct it. On a CONTINUING turn (see below) the title is not edited,
so put the tier at the very start of the new description in square brackets, e.g. "[low] and how
many are in stock?" — that bracket beats the title word for that turn.

CONTINUING A THREAD (whiteboard). The event is a slot holding only the current turn. To ask the
next question in the same thread, EDIT THE SAME EVENT and replace the description with the new
question in ONE edit (never wipe first and write later). Woolly's reply replaces the description
again. I only ever see the latest turn; Woolly keeps the full transcript on its side and restates
what matters in each reply. A brand-new topic gets a brand-new event.

READING REPLIES. Scan today's and yesterday's events on the Woolly calendar whose title starts
with ✓ or ?; read the description. A ⏳ title with words after it ("⏳ step 2 of 3 · …") is a
progress heartbeat: the job is still running, the description is still my question.

CONTEXT NOTES. "Note: <name>" events on 1 Jan 2000 (all-day) are passive context, never requests.
To load one: search the Woolly calendar for the exact title "Note: <name>" with the time window
pinned to 1 Jan 2000, read its description silently as context. Fuzzy name → read "Note: Index"
first and pick one; if still ambiguous, offer the candidate titles, never the bodies.
```
