# Active Locals Events Automation

Researches ActiveLocals community clubs with Claude and prefills the "Create Event" form on
the admin portal via Playwright, reading unprocessed rows straight from the shared Google
Sheet. A human still reviews and submits each form in the browser — this automates the
research-and-typing, not the judgment call.

## Setup

```bash
python3 -m venv env
env/bin/pip install -r requirements.txt
env/bin/playwright install chromium
cp .env.example .env   # then fill in your own values
```

`.env` needs:
- `ACTIVELOCALS_EMAIL` / `ACTIVELOCALS_PASSWORD` — your admin portal login
- `ANTHROPIC_API_KEY` — used to research each club

`.env` is git-ignored. Never commit real credentials.

## Usage

Batch mode — processes every row in the sheet that doesn't already have a status set:

```bash
env/bin/python batch_create_events.py
```

Single club:

```bash
env/bin/python test_single_event.py --club-id "<club_id>" --club-name "<club name>"
```

Either way: the script opens a real browser window, logs in, fills the form, then pauses for
you to review and click Submit yourself before moving to the next club.

## Using this from Claude Code

This repo includes a Claude Code skill (`.claude/skills/create-al-event`). Open this directory
in Claude Code and run `/create-al-event` — it handles the environment setup (venv,
dependencies, Playwright browser, asking for your `.env` credentials if missing) and then runs
the automation above, relaying each review/submit prompt to you.
