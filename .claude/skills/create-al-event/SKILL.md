---
name: create-al-event
description: Run the ActiveLocals event-creation automation (test_single_event.py / batch_create_events.py) to research a club with Claude and prefill its Create Event form in the admin portal via Playwright. Use when the user asks to create/prefill an event for a specific club, or to run the batch club import from the Google Sheet.
---

This skill drives the Playwright automation in `/home/mvzaveri/ActiveLocals/Liquidity` that
researches a club and prefills its "Create Event" form on the ActiveLocals admin portal.
Login and the form fields still require the user to visually confirm/submit in the browser -
this automates the tedious research + typing, not the final submit.

## Usage

- `/create-al-event <club_id> <club_name>` — research one specific club and prefill its event form.
- `/create-al-event batch` — pull all unfilled rows from the Google Sheet and process them one by one.
- `/create-al-event` (no args) — run the single-club script using whatever CLUB_ID/CLUB_NAME are
  currently hardcoded at the top of `test_single_event.py` (mainly useful for the maintainer's own testing).

## Before running

1. Check `ANTHROPIC_API_KEY` is available: `echo $ANTHROPIC_API_KEY`. If empty, ask the user for
   it — without it, club research silently falls back to an empty skeleton event instead of failing loudly.
2. Login credentials (ACTIVELOCALS_EMAIL / ACTIVELOCALS_PASSWORD) are loaded automatically from
   `.env` in this directory — do not ask the user for these, and do not print their values.
3. `.env` is git-ignored. If this project is ever turned into a git repo, double check `.env` is
   not staged before any commit.

## Running it

Always invoke through the project's venv interpreter, from the project directory:

Single club:
```
cd /home/mvzaveri/ActiveLocals/Liquidity && env/bin/python test_single_event.py --club-id "<club_id>" --club-name "<club name>"
```

Batch (reads directly from the Google Sheet, does not write back to it):
```
cd /home/mvzaveri/ActiveLocals/Liquidity && env/bin/python batch_create_events.py
```

No-args single run (uses the hardcoded CLUB_ID/CLUB_NAME/MANUAL_OVERRIDE in the script):
```
cd /home/mvzaveri/ActiveLocals/Liquidity && env/bin/python test_single_event.py
```

## While it's running

- A real (non-headless) Chromium window opens. Login is attempted automatically; if it fails the
  script falls back to waiting up to 5 minutes for the user to log in by hand — tell the user if
  that happens.
- The script pauses at the terminal (`input(...)` prompts) for the user to review the filled form
  in the browser and manually click submit/confirm themselves, and again to type optional
  free-text corrections (e.g. "every Monday at 5:15am at Olivers Hill Frankston"). Relay these
  prompts to the user and wait for their reply before doing anything else — do not attempt to
  auto-answer them.
- Never click "Confirm"/"Submit" in the browser yourself and never send Enter at those prompts on
  the user's behalf — the manual review step exists specifically so a human checks the data before
  it goes live.

## After it finishes

Report which club(s) were processed, and flag anything the script marked with:
- 🚩 — missing day/time, needs manual scheduling
- ⚠️ — missing address or image, needs manual fill-in
