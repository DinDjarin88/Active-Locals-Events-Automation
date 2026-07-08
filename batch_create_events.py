"""
ActiveLocals - Batch Event Creator (Google Sheet driven, READ-ONLY on the sheet)
==================================================================================
Reads clubId/groupName rows from the shared Google Sheet, also reads past events
and description columns to extract recurring day/time patterns (e.g. "every Monday
at 6:30am"), skips any row that already has a status, and for every remaining row:
researches the club with Claude, opens the Create Event form and prefills everything
found (title, description, address, date/time if known or extracted from "every..."),
intensity).

MANUAL SUBMIT FLOW:
- The script fills the form
- You manually upload the image, fix anything needed, click Review & Confirm,
  and click Confirm in the browser yourself
- Only after the event is actually submitted do you press Enter in the terminal
- The script then moves to the next club automatically

This script does NOT write anything back to the Google Sheet - you update
the status column yourself.

Usage:
    Either:
    A) Make sure ANTHROPIC_API_KEY is set, then: python batch_create_events.py
       (each club is researched live via the Anthropic API)
    B) python batch_create_events.py --research-file research.json
       where research.json is {"<club_id>": {...event fields...}, ...} - no API key
       needed, e.g. when a Claude Code agent has already researched every pending
       club itself using the identical prompt/rules from research_club_with_claude().
       Any club_id missing from the file falls back to the same blank-skeleton
       behaviour as a failed API call, exactly as it always has.

    Then: log in manually in the browser window when prompted (once), and for each
    club let the script prefill the form, manually submit in the browser, then press
    Enter in the terminal to continue.
"""

import os
import csv
import io
import re
import json
import argparse
import requests

from playwright.sync_api import sync_playwright

import test_single_event as tse

tse.MANUAL_OVERRIDE = None

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
# Fallback used only if no sheet is provided via --sheet-url / prompt - keeps this
# script runnable exactly as before for anyone who doesn't pass one.
DEFAULT_SHEET_ID = "1PaLF2yFwNEy9f85oHd2_BqIrcS3qaslNngTloFFnDLg"
DEFAULT_SHEET_GID = "0"

MAX_CLUBS_THIS_RUN = None


def build_csv_export_url(sheet_url_or_id):
    """
    Accepts a full Google Sheets URL (edit link, share link, with or without a gid),
    or a bare sheet ID, and returns the CSV export URL for it.
    """
    sheet_url_or_id = (sheet_url_or_id or "").strip()
    if not sheet_url_or_id:
        return f"https://docs.google.com/spreadsheets/d/{DEFAULT_SHEET_ID}/export?format=csv&gid={DEFAULT_SHEET_GID}"

    id_match = re.search(r"/d/([a-zA-Z0-9-_]+)", sheet_url_or_id)
    sheet_id = id_match.group(1) if id_match else sheet_url_or_id

    gid_match = re.search(r"[?#&]gid=(\d+)", sheet_url_or_id)
    gid = gid_match.group(1) if gid_match else "0"

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


# ─────────────────────────────────────────────
# SHEET READING (read-only)
# ─────────────────────────────────────────────

def _is_blank(value):
    if value is None:
        return True
    stripped = str(value).replace("\u200b", "").strip()
    return stripped == ""


def extract_day_time_from_text(text):
    """
    Tries to extract day-of-week and time from text like:
    'every Monday at 6:30am', 'Tuesdays 10:00', 'Wed 7pm', etc.
    Returns: (day_of_week, start_time_24h) or (None, None) if not found.
    """
    if not text:
        return None, None

    text_lower = text.lower()
    
    # Day patterns
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    found_day = None
    for day in days:
        if day in text_lower:
            found_day = day.capitalize()
            break
    
    if not found_day:
        return None, None
    
    # Time patterns: "6:30am", "6:30 am", "6:30", "1800", etc.
    time_match = re.search(r'(\d{1,2}):?(\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?', text_lower)
    
    if not time_match:
        return found_day, None
    
    hour = int(time_match.group(1))
    minute = int(time_match.group(2)) if time_match.group(2) else 0
    meridiem = time_match.group(3)
    
    # Convert to 24-hour format
    if meridiem and 'p' in meridiem:
        if hour != 12:
            hour += 12
    elif meridiem and 'a' in meridiem:
        if hour == 12:
            hour = 0
    
    time_24h = f"{hour:02d}:{minute:02d}"
    
    return found_day, time_24h


def load_pending_rows(sheet_url_or_id=None):
    csv_export_url = build_csv_export_url(sheet_url_or_id)
    print(f"📥 Loading sheet: {csv_export_url}")
    resp = requests.get(csv_export_url, timeout=20)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)

    if not rows:
        print("  ⚠️  Sheet appears empty.")
        return []

    # Try to find columns by header (case-insensitive)
    header = rows[0] if rows else []
    header_lower = [h.strip().lower() for h in header]
    
    # Map common column names
    col_id = 0
    col_name = 1
    col_status = 3
    col_past_events = -1
    col_description = -1
    
    for i, h in enumerate(header_lower):
        if "clubid" in h or "club id" in h:
            col_id = i
        elif "groupname" in h or "group name" in h or "name" in h:
            col_name = i
        elif "event created" in h or "status" in h:
            col_status = i
        elif "past events" in h:
            col_past_events = i
        elif "description" in h:
            col_description = i

    data_rows = rows[1:]

    pending = []
    for idx, row in enumerate(data_rows):
        row_num = idx + 2

        club_id = row[col_id].strip() if col_id < len(row) and row[col_id] else ""
        club_name = row[col_name].strip() if col_name < len(row) and row[col_name] else ""
        status = row[col_status].strip() if col_status < len(row) else ""
        
        past_events = row[col_past_events].strip() if col_past_events >= 0 and col_past_events < len(row) else ""
        description = row[col_description].strip() if col_description >= 0 and col_description < len(row) else ""

        if not club_id or not club_name:
            continue
        if not _is_blank(status):
            continue

        # Try to extract day/time from "past events" or "description"
        day_of_week, start_time = extract_day_time_from_text(past_events)
        if not day_of_week:
            day_of_week, start_time = extract_day_time_from_text(description)

        pending.append({
            "row_num": row_num,
            "club_id": club_id,
            "club_name": club_name,
            "past_events": past_events,
            "description": description,
            "day_of_week": day_of_week or "",
            "start_time": start_time or "",
        })

    print(f"  ✅ Found {len(pending)} unprocessed club(s) out of {len(data_rows)} total rows.")
    return pending


# ─────────────────────────────────────────────
# MAIN BATCH LOOP
# ─────────────────────────────────────────────

def run_batch(research_file=None, sheet_url=None):
    research_map = {}
    if research_file:
        with open(research_file) as f:
            research_map = json.load(f)
        print(f"📚 Loaded pre-researched data for {len(research_map)} club(s) from {research_file}")

    if not sheet_url:
        sheet_url = input("📋 Paste the Google Sheet link to process: ").strip()

    pending = load_pending_rows(sheet_url)
    if not pending:
        print("Nothing to do - all rows already have a status, or the sheet is empty.")
        return

    if MAX_CLUBS_THIS_RUN:
        pending = pending[:MAX_CLUBS_THIS_RUN]
        print(f"  (Limiting to first {MAX_CLUBS_THIS_RUN} for this run)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=60, args=["--start-maximized"])
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        if not tse.ensure_logged_in(page):
            print("❌ Could not log in. Exiting.")
            return

        i = 0
        while i < len(pending):
            row = pending[i]
            club_id = row["club_id"]
            club_name = row["club_name"]
            row_num = row["row_num"]
            day_of_week = row["day_of_week"]
            start_time = row["start_time"]

            print("\n" + "═" * 70)
            print(f"[{i+1}/{len(pending)}] {club_name}  (row {row_num}, id {club_id})")
            if day_of_week and start_time:
                print(f"     Extracted: every {day_of_week} at {start_time}")
            print("  Commands after form fill: Enter=next  s=skip  b=back")
            print("═" * 70)

            if club_id in research_map:
                print(f"  ✅ Using pre-researched data for: {club_name}")
                event = dict(research_map[club_id])
            elif research_file:
                # --research-file mode never calls the API, even for a club it
                # doesn't cover - that would silently reintroduce the API dependency.
                print(f"  ⚠️  No pre-researched data for {club_id} - opening blank form for manual entry.")
                event = None
            else:
                event = tse.research_club_with_claude(club_name)

            if not event:
                if not research_file:
                    print("  ⚠️  Claude research failed - opening blank form for manual entry.")
                event = {
                    "title": club_name,
                    "description": "",
                    "what_to_expect": "",
                    "website": "",
                    "intensity": "Just for Fun",
                    "address": "",
                    "day_of_week": "",
                    "start_time": "",
                    "end_time": "",
                    "recurring": True,
                    "image_search_query": club_name,
                    "start_dt": "",
                    "end_dt": "",
                }

            # If we extracted a day/time from the sheet, prefer that over Claude's guess
            if day_of_week:
                event["day_of_week"] = day_of_week
            if start_time:
                event["start_time"] = start_time
                if not event.get("end_time"):
                    h, m = map(int, start_time.split(":"))
                    h = (h + 1) % 24
                    event["end_time"] = f"{h:02d}:{m:02d}"

            event = tse.resolve_event_datetimes(event, interactive=False)

            print("\n📋 Event details (will prefill into the form):")
            for k, v in event.items():
                if v:
                    print(f"   {k}: {v}")

            ok, scraped = tse.navigate_to_club(page, club_id)
            if not ok:
                print("  ❌ Could not reach club admin page - skipping.")
                i += 1
                continue

            scraped_text = scraped.get("description_text", "")
            page_state = scraped.get("page_state", "")
            page_location = scraped.get("page_location", "")

            specific_address = None
            if scraped_text and page_state:
                specific_address = tse.extract_meeting_point(scraped_text, page_state)
                if specific_address:
                    print(f"  📍 Meeting point from description: {specific_address}")

            if specific_address:
                event["address"] = specific_address
            elif page_location:
                claude_address = event.get("address", "")
                if not claude_address:
                    print(f"  📍 No address from Claude - using page location: {page_location}")
                    event["address"] = page_location
                elif page_state and page_state.upper() not in claude_address.upper():
                    print(f"  📍 Claude address wrong state (expected {page_state}), using page location: {page_location}")
                    event["address"] = page_location

            if not event.get("address") and page_location:
                print(f"  📍 Falling back to page location: {page_location}")
                event["address"] = page_location

            if scraped_text:
                day, start_t, end_t = tse.extract_schedule_from_text(scraped_text)
                if day:
                    print(f"  📅 Extracted from page: every {day} {start_t or '?'} - {end_t or '?'}")
                    event["day_of_week"] = day
                    if start_t:
                        event["start_time"] = start_t
                    if end_t:
                        event["end_time"] = end_t
                    elif start_t and not event.get("end_time"):
                        h, m = map(int, start_t.split(":"))
                        event["end_time"] = f"{(h+1)%24:02d}:{m:02d}"
                    event.pop("start_dt", None)
                    event.pop("end_dt", None)
                    event = tse.resolve_event_datetimes(event, interactive=False)
                    print(f"     → {event.get('start_dt')} to {event.get('end_dt')}")

            image_query = event.get("image_search_query", club_name)
            cover_image_url = scraped.get("cover_image_url", "")
            cover_filename = f"{club_id}.jpg"
            cover_path = os.path.join(tse.IMAGE_DIR, cover_filename)

            if os.path.exists(cover_path):
                print(f"  📷 Using cached image: {cover_filename}")
                image_path = cover_path
            elif cover_image_url:
                print(f"  🖼️  Using club's own page image...")
                image_path = tse.download_page_cover_image(cover_image_url, tse.IMAGE_DIR, cover_filename)
                if not image_path:
                    image_path = tse.download_image(club_name=club_name, query=image_query,
                                                     save_dir=tse.IMAGE_DIR, filename=cover_filename, location=page_location)
            else:
                image_path = tse.download_image(club_name=club_name, query=image_query,
                                                 save_dir=tse.IMAGE_DIR, filename=cover_filename, location=page_location)

            try:
                result = tse.create_event_for_club(page, event, image_path=image_path, pause_for_review=True, page_state=page_state)
            except Exception as e:
                print(f"  ❌ Error while filling form: {e}")
                print("  ⚠️  Please fix anything needed in the browser.")
                result = input("   Enter=next  s=skip  b=back: ").strip().lower()

            if result == "b" and i > 0:
                print(f"  ⏪ Going back to previous club...")
                i -= 1
            elif result == "s":
                print(f"  ⏭️  Skipping...")
                i += 1
            else:
                i += 1

            print("  ➡️  (Remember: update the sheet yourself.)")

        print("\n🎉 Batch run complete!")
        input("Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch-process unprocessed ActiveLocals clubs from the sheet.")
    parser.add_argument(
        "--research-file",
        help="Path to a JSON file of {club_id: event_fields} - skips the Anthropic API "
             "entirely, e.g. when a Claude Code agent has already researched every "
             "pending club itself.",
    )
    parser.add_argument(
        "--sheet-url",
        help="Google Sheet link (or bare sheet ID) to read pending clubs from. "
             "If omitted, you'll be prompted for it.",
    )
    args = parser.parse_args()
    run_batch(research_file=args.research_file, sheet_url=args.sheet_url)
