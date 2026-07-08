---
name: create-al-event
description: Run the ActiveLocals event-creation automation entirely inside Claude Code - the agent bootstraps the environment, researches each unprocessed club itself (no Anthropic API key needed), then drives the Playwright automation in the background and relays each review/submit/skip decision through the chat, so the user never has to leave Claude Code or open a separate terminal. Use when the user asks to process/upload/create events for ActiveLocals clubs, or to run/test this automation.
---

This skill drives the Playwright automation in this repository that researches ActiveLocals
clubs and prefills their "Create Event" form on the admin portal - end to end, inside this
Claude Code session. It never submits anything itself - a human always reviews the filled form
in the browser and clicks Confirm/Submit, then tells you (in chat) to move on. That is by
design: the automation removes the tedious research-and-typing, not the judgment call of
whether the data is correct, or the human's control over when to advance.

**How you run it without a separate terminal:** the script blocks on `input()` for the
review/submit step, and your Bash tool doesn't give the user a real shared terminal - so you
keep the script's stdin open yourself via a FIFO, run the script in the background, and relay
the user's chat replies ("next" / "skip" / "back" / free-text corrections) into that FIFO. This
is standard POSIX (`mkfifo`, background jobs) and works the same on Linux, macOS, and WSL. The
exact mechanics are in Step 4 - follow them precisely, this is the part that's easy to get
subtly wrong (buffering, EOF-closing the pipe, etc).

Research (step 3) is done by **you**, not by a separate Anthropic API call - so no
`ANTHROPIC_API_KEY` is required. Follow the exact research contract below so behaviour matches
the original API-based version byte-for-byte in shape and rules (same JSON schema, same "don't
guess if unsure" behaviour). The original never did live web search either - it only used the
model's own knowledge - so you should do the same: answer from what you already know, don't go
run web searches to compensate.

## Usage

- `/create-al-event` — batch mode (the normal path). Pulls every row from the shared Google
  Sheet that doesn't already have a status set, and processes them one at a time entirely in
  this chat: you research → prefill form → user reviews and submits in the browser → they tell
  you "next" in chat → you advance to the next club.
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
4. Check for a `.env` file in the repo root. **This step never blocks progress** —
   `ensure_logged_in()` in `test_single_event.py` already falls back to opening the real
   Chromium window at the login page and waiting (up to 5 minutes) for the user to log in by
   hand if credentials aren't set, so a missing `.env` is not an error condition.
   - If `.env` is missing, just mention once, in passing, that she can either log in manually
     in the browser window when it opens, or give you her ActiveLocals email/password now to
     save into `.env` (git-ignored — verify with `git check-ignore -v .env` before ever running
     `git add`) for automatic login on future runs. Then move on to Step 1 immediately — do not
     wait for an answer before proceeding.
   - If she does offer credentials later in the conversation, write them to `.env` using the
     keys `ACTIVELOCALS_EMAIL`, `ACTIVELOCALS_PASSWORD` (see `.env.example` for the template).
     Ask for them as a plain chat message if you ever do need to prompt — do **not** use a
     structured question/option-picker tool for this, since email/password are free text, not a
     choice between alternatives.
   - Never hardcode a specific person's credentials into any tracked file, and never print the
     password back out once it's saved.

## Step 1 — ask for the Google Sheet link (batch mode only, skip for a single club)

Ask the user to paste the link to the Google Sheet to process (a normal share/edit link is
fine — the tool accepts a full URL with or without a `gid`, or a bare sheet ID). Don't assume
or reuse a sheet from a previous run; ask fresh each time in case they're pointing at a
different one. Keep the exact string they give you - you'll pass it verbatim as `--sheet-url`
in both step 2's read and step 4's launch command below, so both operate on the same sheet.

## Step 2 — read the pending clubs (no API key involved, just reads the sheet)

```
env/bin/python -c "import batch_create_events as bce, json; print(json.dumps(bce.load_pending_rows('<the sheet link from step 1>')))"
```

This returns a JSON list of clubs that don't already have a status set in the sheet, each with
`club_id`, `club_name`, and any `day_of_week`/`start_time` already extracted from the sheet's
own text columns.

## Step 3 — research each pending club yourself

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

Write the results to a file in the repo root, e.g. `research_run.json`, as
`{"<club_id>": {...fields...}, ...}` for every pending club (skip a club entirely, or leave its
fields empty, if you're not confident — the automation already opens a blank/partial form for
manual entry for anything you skip). Add that filename to `.gitignore` if it isn't already
covered, so it never gets committed.

## Step 4 — run it in the background and relay control through chat

**Set up a FIFO so the script's stdin never closes** (closing it makes Python raise `EOFError`
the next time it calls `input()`, which would silently kill the run mid-club):

```
mkfifo /tmp/al_stdin.fifo
nohup tail -f /dev/null > /tmp/al_stdin.fifo &
disown
```

**Launch the script in the background, unbuffered (`-u`) so log output shows up immediately**
instead of sitting in a buffer until the process exits (this bit you in earlier testing - don't
skip `-u`):

```
env/bin/python -u batch_create_events.py --sheet-url "<sheet link from step 1>" --research-file research_run.json < /tmp/al_stdin.fifo > /tmp/al_run.log 2>&1 &
disown
```

**Poll the log** (`tail -n 40 /tmp/al_run.log`) to see where it's at, and narrate progress to
the user as it happens (logged in, navigated to club X, filled the form, etc) rather than going
silent until something needs their input.

**When it reaches a pause** ("Form filled. Please review in the browser" / "Press Enter to move
to the next club"), tell the user in chat what's ready for review and wait for their reply.
Do not send anything into the FIFO until they've actually responded - never auto-advance.

**Translate their reply into the FIFO:**
- "looks good" / "next" / "submitted" → `printf '\n' > /tmp/al_stdin.fifo`
- "skip" → `printf 's\n' > /tmp/al_stdin.fifo`
- "go back" → `printf 'b\n' > /tmp/al_stdin.fifo`
- Free-text corrections (e.g. "it's actually at Centennial Park") → `printf '%s\n' "<their text>" > /tmp/al_stdin.fifo`

Then poll the log again and repeat until the run reports "Batch run complete!" and pauses one
last time to close the browser - confirm with the user before sending that final Enter too.

**Never** click Confirm/Submit in the browser yourself, and **never** send input into the FIFO
that the user didn't just ask for - the whole point of relaying is that nothing advances without
them explicitly saying so.

**Cleanup when done or if the user wants to stop:** send `SIGINT` (not `SIGTERM`/`SIGKILL`) to
the python process so Playwright's context manager closes the browser gracefully, then kill the
`tail` sentinel and remove the FIFO.

**If any of this fails** (e.g. `mkfifo` isn't available in the user's environment), fall back to
handing them the same command to run themselves in their own terminal instead - tell them
explicitly why you're falling back.

`batch_create_events.py` never writes back to the sheet. After each club is actually submitted
in the browser, remind the user to mark it processed in the sheet's status column themselves,
or it'll be re-offered next run.

(An `ANTHROPIC_API_KEY` path still exists as a fallback for anyone who wants the script to
research clubs itself via the Anthropic API instead of steps 2-3 above — just omit
`--research-file` and set the key. `--sheet-url` still works either way.)

## After it finishes

Summarize which club(s) were actually submitted vs skipped, and flag anything you marked with:
- 🚩 — missing day/time, needs manual scheduling
- ⚠️ — missing address or other field you left blank due to low confidence, needs manual fill-in

Remind the user to update the sheet's status column for anything they submitted, if they
haven't already.
