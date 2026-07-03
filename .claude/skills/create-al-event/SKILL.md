---
name: create-al-event
description: Run the ActiveLocals event-creation automation against the shared Google Sheet - the Claude Code agent researches each unprocessed club itself (no Anthropic API key needed) and prefills its Create Event form in the admin portal via Playwright, so a human only has to validate and click submit before moving to the next one. Use when the user asks to process/upload/create events for ActiveLocals clubs, or to run/test this automation.
---

This skill drives the Playwright automation in this repository that researches ActiveLocals
clubs and prefills their "Create Event" form on the admin portal. It does **not** submit
anything itself - a human always reviews the filled form in the browser and clicks
Confirm/Submit, then tells the script (via the terminal prompt) to move to the next club.
That is by design: the automation removes the tedious research-and-typing, not the judgment
call of whether the data is correct.

Research is done by **you** (the Claude Code agent running this skill), not by a separate
Anthropic API call - so no `ANTHROPIC_API_KEY` is required. You must follow the exact
research contract below so behaviour matches the original API-based version byte-for-byte
in shape and rules (same JSON schema, same "don't guess if unsure" behaviour). The original
never did live web search either - it only used the model's own knowledge - so you should
do the same: answer from what you already know, don't go run web searches to compensate.

## Usage

- `/create-al-event` — batch mode (the normal path). Pulls every row from the shared Google
  Sheet that doesn't already have a status set, and processes them one at a time: you research
  → prefill form → human validates and submits in the browser → Enter in the terminal → next club.
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
   - Ask the user for their ActiveLocals admin portal email and password.
   - Write them to `.env` (already git-ignored — verify with `git check-ignore -v .env` before
     ever running `git add`) using the keys `ACTIVELOCALS_EMAIL`, `ACTIVELOCALS_PASSWORD`.
     Use `.env.example` as the template. `ANTHROPIC_API_KEY` is not needed for this flow.
   - Never hardcode a specific person's credentials into any tracked file, and never print the
     password back out once it's saved.

## Step 1 — read the pending clubs (no API key involved, just reads the sheet)

```
env/bin/python -c "import batch_create_events as bce, json; print(json.dumps(bce.load_pending_rows()))"
```

This returns a JSON list of clubs that don't already have a status set in the sheet, each with
`club_id`, `club_name`, and any `day_of_week`/`start_time` already extracted from the sheet's
own text columns.

## Step 2 — research each pending club yourself

For every club in that list, produce a JSON object using **exactly** this contract (this is the
original system prompt verbatim - follow it exactly, do not embellish or add your own rules):

> You are an assistant that researches community groups and generates event details for the
> ActiveLocals platform.
>
> When given a club name, use your knowledge to find information about that club, then return a
> JSON object with these exact fields:
> ```json
> {
>   "title": "Event title (e.g. 'Chilli Chicks Run Club, Wednesday Morning Run')",
>   "description": "2-3 sentence description of the club and what the event is",
>   "what_to_expect": "2-3 sentences on what attendees can expect at the session",
>   "website": "Club website or social media URL, or empty string if none found",
>   "intensity": "One of: Just for Fun | Fit & Focused | High Performance",
>   "address": "Full street address of where the event is held. Must be a real, searchable address.",
>   "day_of_week": "The day of the week this recurring event happens, e.g. 'Wednesday'. If you are not confident, return an empty string.",
>   "start_time": "Start time in 24h HH:MM local time at the venue, e.g. '06:30'. If not confident, return an empty string.",
>   "end_time": "End time in 24h HH:MM local time at the venue, e.g. '07:30'. If not confident, return an empty string.",
>   "recurring": true,
>   "image_search_query": "The exact club name - this will be used to search Google Images for photos of this specific group."
> }
> ```
> Rules:
> - No em dashes anywhere
> - Keep descriptions factual and friendly
> - If you are not confident about the day_of_week or times, return empty strings for those
>   fields rather than guessing - the tool will prompt a human to fill them in
> - For ADDRESS: Include a specific street number and name, not just a park name or suburb
> - For image_search_query: Return the EXACT CLUB NAME from search results, not a generic query
> - Return ONLY the JSON object, no other text, no markdown code blocks

If the sheet already extracted a `day_of_week`/`start_time` for a club, prefer that over your
own guess for those two fields (same precedence the original script already applies).

Write the results to a scratch file as `{"<club_id>": {...fields...}, ...}` for every pending
club (skip a club entirely, or leave its fields empty, if you're not confident — the automation
already opens a blank/partial form for manual entry for anything you skip).

## Step 3 — run the automation with your research

Batch mode:
```
env/bin/python batch_create_events.py --research-file <path to the JSON from step 2>
```

Single club:
```
env/bin/python test_single_event.py --club-id "<club_id>" --club-name "<club name>" --event-json-file <path to a one-club JSON file with the same fields>
```

(An `ANTHROPIC_API_KEY` path still exists as a fallback for anyone who wants the script to
research clubs itself via the Anthropic API instead — just omit `--research-file`/
`--event-json-file` and set the key. Not needed for the normal Claude Code flow above.)

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
- ⚠️ — missing address, image, or pre-researched data, needs manual fill-in
