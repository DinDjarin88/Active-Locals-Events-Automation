---
name: create-al-event
description: Run the ActiveLocals event-creation automation against the shared Google Sheet - researches each unprocessed club with Claude and prefills its Create Event form in the admin portal via Playwright, so a human only has to validate and click submit before moving to the next one. Use when the user asks to process/upload/create events for ActiveLocals clubs, or to run/test this automation.
---

This skill drives the Playwright automation in this repository that researches ActiveLocals
clubs and prefills their "Create Event" form on the admin portal. It does **not** submit
anything itself - a human always reviews the filled form in the browser and clicks
Confirm/Submit, then tells the script (via the terminal prompt) to move to the next club.
That is by design: the automation removes the tedious research-and-typing, not the judgment
call of whether the data is correct.

## Usage

- `/create-al-event` — batch mode (the normal path). Pulls every row from the shared Google
  Sheet that doesn't already have a status set, and processes them one at a time: research →
  prefill form → human validates and submits in the browser → Enter in the terminal → next club.
- `/create-al-event <club_id> <club_name>` — process one specific club only (useful for
  re-running a single row, or testing).

## Step 0 — environment bootstrap (run every time, each check is cheap/idempotent)

Run these from the repository root (`cd "$(git rev-parse --show-toplevel)"` — do not hardcode
an absolute path, this repo may be cloned anywhere):

1. Create the venv if it doesn't exist yet: `test -d env || python3 -m venv env`
2. Install/refresh dependencies: `env/bin/pip install -q -r requirements.txt`
3. Make sure the Playwright Chromium build is installed: `env/bin/playwright install chromium`
   (this is a no-op if already cached, so safe to run unconditionally). If browser launch fails
   later with missing shared-library errors, that means Linux system deps are missing — tell the
   user to run `env/bin/playwright install --with-deps chromium` themselves (installs system
   packages via sudo, so don't run it on their behalf without asking).
4. Check for a `.env` file in the repo root. If it's missing:
   - Ask the user for their ActiveLocals admin portal email and password, and for an
     `ANTHROPIC_API_KEY` if one isn't already set in the shell environment.
   - Write them to `.env` (already git-ignored — verify with `git check-ignore -v .env` before
     ever running `git add`) using the keys `ACTIVELOCALS_EMAIL`, `ACTIVELOCALS_PASSWORD`,
     `ANTHROPIC_API_KEY`. Use `.env.example` as the template.
   - Never hardcode a specific person's credentials into any tracked file, and never print the
     password back out once it's saved.

## Step 1 — run it

Batch mode (default):
```
env/bin/python batch_create_events.py
```

Single club:
```
env/bin/python test_single_event.py --club-id "<club_id>" --club-name "<club name>"
```

## While it's running

- A real (non-headless) Chromium window opens. Login is attempted automatically from `.env`;
  if that fails the script falls back to waiting up to 5 minutes for the user to log in by hand
  — tell the user if that happens.
- The script pauses at the terminal for the user to review the filled form in the browser and
  manually submit it themselves, and again to type optional free-text corrections (e.g. "every
  Monday at 5:15am at Olivers Hill Frankston") or `s`/`b` to skip/go back a club. Relay these
  prompts to the user and wait for their reply before doing anything else - do not auto-answer
  them or send blank Enters on their behalf to rush through clubs.
- Never click "Confirm"/"Submit" in the browser yourself and never send Enter at those prompts
  without the user telling you to — the manual review step exists specifically so a human checks
  the data before it goes live.
- `batch_create_events.py` never writes back to the sheet. After each club is actually
  submitted in the browser, remind the user to mark it processed in the sheet's status column
  themselves, or it will be re-offered next run.

## After it finishes

Report which club(s) were processed, and flag anything the script marked with:
- 🚩 — missing day/time, needs manual scheduling
- ⚠️ — missing address or image, needs manual fill-in
