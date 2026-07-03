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
- `ANTHROPIC_API_KEY` — optional, see below

`.env` is git-ignored. Never commit real credentials.

## Usage

Neither script researches clubs itself unless you let it — they can either call the Anthropic
API directly, or take pre-researched data you supply, so no API key is required at all:

Batch mode — processes every row in the sheet that doesn't already have a status set:

```bash
env/bin/python batch_create_events.py                                    # needs ANTHROPIC_API_KEY
env/bin/python batch_create_events.py --research-file research.json      # no API key needed
```

Single club:

```bash
env/bin/python test_single_event.py --club-id "<id>" --club-name "<name>"                              # needs ANTHROPIC_API_KEY
env/bin/python test_single_event.py --club-id "<id>" --club-name "<name>" --event-json-file event.json  # no API key needed
```

`research.json` / `event.json` follow the same field shape documented in
`.claude/skills/create-al-event/SKILL.md`. Either way: the script opens a real browser window,
logs in, fills the form, then pauses for you to review and click Submit yourself before moving
to the next club.

## Using this from Claude Code (recommended, no API key needed)

This repo includes a Claude Code skill (`.claude/skills/create-al-event`). Open this directory
in Claude Code and run `/create-al-event` — it handles the environment setup (venv,
dependencies, Playwright browser, asking for your `.env` credentials if missing), researches
each pending club itself using the same rules the original API prompt used, then runs the
automation above with that research, relaying each review/submit prompt to you. No
`ANTHROPIC_API_KEY` required for this path.
