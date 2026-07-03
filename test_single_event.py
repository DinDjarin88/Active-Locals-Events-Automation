"""
ActiveLocals - Single Event Test Script (with Claude research + address autocomplete)
======================================================================================
Usage:
    1. python test_single_event.py                                    (uses CLUB_ID/CLUB_NAME below)
    2. python test_single_event.py --club-id ID --club-name "Name"    (overrides them, runs live research)
    3. Claude will research the club and generate event details automatically

Credentials (ACTIVELOCALS_EMAIL, ACTIVELOCALS_PASSWORD, ANTHROPIC_API_KEY) are read from
the environment, or from a local .env file (same directory, git-ignored) if present.
"""

import os
import re
import time
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


def _load_dotenv():
    """Minimal .env loader (no external dependency) - does not override real env vars."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# ─────────────────────────────────────────────
# CREDENTIALS — read from environment / .env, never hardcode here
# ─────────────────────────────────────────────
EMAIL = os.getenv("ACTIVELOCALS_EMAIL", "")
PASSWORD = os.getenv("ACTIVELOCALS_PASSWORD", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "sk-ant-YOUR_KEY_HERE")

BASE_URL = "https://mapqhipy2m.ap-southeast-2.awsapprunner.com"

# ─────────────────────────────────────────────
# CLUB TO PROCESS — only used when running this script directly with no
# --club-id/--club-name flags. Leave blank; batch_create_events.py (the
# normal entry point) always overrides these from the Google Sheet.
# ─────────────────────────────────────────────
CLUB_ID   = ""
CLUB_NAME = ""

# Optional: Override Claude's research with correct details (leave as None to use Claude).
# Only ever set this temporarily on your own machine for debugging one club - never commit
# real values here, since anyone else running the script would silently get your fixture data.
# Format: dict with keys: title, description, what_to_expect, website, intensity, address,
#         day_of_week, start_time, end_time, recurring, image_search_query
# day_of_week: one of Monday/Tuesday/.../Sunday, or "" if unknown (will prompt for manual entry)
# start_time / end_time: 24h "HH:MM" local time
MANUAL_OVERRIDE = None

IMAGE_DIR = "./temp_images"

# ─────────────────────────────────────────────
# DATE/TIME RESOLUTION
# ─────────────────────────────────────────────

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def resolve_event_datetimes(event, interactive=True):
    """
    Fills in event['start_dt'] and event['end_dt'] (YYYY-MM-DDTHH:MM) based on
    day_of_week/start_time/end_time, always picking the NEXT upcoming occurrence.
    If day_of_week or times are missing/invalid:
      - interactive=True: prompts the terminal for manual entry (blocking)
      - interactive=False: flags it and leaves start_dt/end_dt blank so the
        browser form can be filled in manually instead (used by batch mode,
        so one unclear club doesn't block the whole run)
    Also handles events that already provide start_dt/end_dt directly (Claude-researched events).
    """
    # If start_dt/end_dt are already provided directly (e.g. from Claude), use those as-is
    if event.get("start_dt") and event.get("end_dt"):
        return event

    day_of_week = (event.get("day_of_week") or "").strip().title()
    start_time = (event.get("start_time") or "").strip()
    end_time = (event.get("end_time") or "").strip()

    valid_day = day_of_week in WEEKDAYS
    valid_start = bool(re.match(r'^\d{1,2}:\d{2}$', start_time))
    valid_end = bool(re.match(r'^\d{1,2}:\d{2}$', end_time))

    if not (valid_day and valid_start and valid_end):
        print("\n  🚩 FLAGGED: Could not determine a valid recurring day/time for this event.")
        print(f"     day_of_week={day_of_week!r}, start_time={start_time!r}, end_time={end_time!r}")

        if not interactive:
            print("     Leaving date/time blank - please fill them in manually in the browser.")
            event["start_dt"] = ""
            event["end_dt"] = ""
            return event

        print("     Please enter these manually.")
        if not valid_day:
            while day_of_week not in WEEKDAYS:
                day_of_week = input(f"     Enter day of week ({'/'.join(WEEKDAYS)}): ").strip().title()
        if not valid_start:
            while not re.match(r'^\d{1,2}:\d{2}$', start_time):
                start_time = input("     Enter start time (24h HH:MM, e.g. 06:30): ").strip()
        if not valid_end:
            while not re.match(r'^\d{1,2}:\d{2}$', end_time):
                end_time = input("     Enter end time (24h HH:MM, e.g. 07:30): ").strip()

    # Compute the next occurrence of that weekday (today counts if the time hasn't passed yet)
    target_weekday_idx = WEEKDAYS.index(day_of_week)
    now = datetime.now()
    days_ahead = (target_weekday_idx - now.weekday()) % 7

    start_h, start_m = map(int, start_time.split(":"))
    if start_h > 23 or start_m > 59:
        print(f"  ⚠️  Invalid start time '{start_time}' - leaving blank")
        event["start_dt"] = ""
        event["end_dt"] = ""
        return event
    candidate_date = now + timedelta(days=days_ahead)
    candidate_start = candidate_date.replace(hour=start_h, minute=start_m, second=0, microsecond=0)

    if days_ahead == 0 and candidate_start <= now:
        # Today's slot already passed - jump to next week
        candidate_date = now + timedelta(days=7)
        candidate_start = candidate_date.replace(hour=start_h, minute=start_m, second=0, microsecond=0)

    end_h, end_m = map(int, end_time.split(":"))
    candidate_end = candidate_start.replace(hour=end_h, minute=end_m)

    event["start_dt"] = candidate_start.strftime("%Y-%m-%dT%H:%M")
    event["end_dt"] = candidate_end.strftime("%Y-%m-%dT%H:%M")
    print(f"  📅 Resolved next occurrence: {event['start_dt']} to {event['end_dt']}")
    return event

# ─────────────────────────────────────────────
# INTENSITY MAP
# ─────────────────────────────────────────────
INTENSITY_MAP = {
    "easy":             "Easy Breezy",
    "easy breezy":      "Easy Breezy",
    "light":            "Light & Lively",
    "light & lively":   "Light & Lively",
    "medium":           "Fit & Focused",
    "fit and focused":  "Fit & Focused",
    "fit & focused":    "Fit & Focused",
    "high":             "High Energy",
    "high energy":      "High Energy",
    "just for fun":     "Easy Breezy",
    "high performance": "High Energy",
}

# ─────────────────────────────────────────────
# ADDRESS VERIFICATION (via OpenStreetMap Nominatim - free, no API key)
# ─────────────────────────────────────────────

def _extract_postcode(text):
    """Extract a 4-digit Australian postcode from a string, if present."""
    match = re.search(r'\b(\d{4})\b', text)
    return match.group(1) if match else None


def _extract_suburb(text):
    """Best-effort extraction of the suburb/locality name (text before state/postcode)."""
    # Grab the first comma-separated token as a rough suburb guess
    return text.split(",")[0].strip().lower()


def verify_address(address):
    """
    Verify a real address exists using OpenStreetMap Nominatim geocoding.
    Validates that the result's postcode (if we have one to compare against)
    actually matches, to avoid false-positive street-name collisions
    (e.g. 'Hawthorne Terrace' matching a query for the suburb 'Hawthorne').
    Returns the verified/normalized address string, or None if not found/verifiable.
    """
    if not address:
        return None

    expected_postcode = _extract_postcode(address)

    headers = {
        # Nominatim requires a descriptive User-Agent
        "User-Agent": "ActiveLocalsEventUploader/1.0 (internal tool)"
    }

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": address,
                "format": "json",
                "countrycodes": "au",
                "limit": 5,  # get a few candidates so we can validate, not just take #1
                "addressdetails": 1,
            },
            headers=headers,
            timeout=10,
        )
        results = resp.json()

        if not results:
            print(f"    ❌ Address not found: {address}")
            return None

        # If we know the expected postcode, require a result that matches it
        if expected_postcode:
            for r in results:
                r_postcode = r.get("address", {}).get("postcode")
                if r_postcode == expected_postcode:
                    verified = r.get("display_name")
                    print(f"    ✅ Verified address (postcode match): {verified}")
                    return verified
            # None of the candidates matched the expected postcode
            print(f"    ❌ Found results but none matched postcode {expected_postcode} for: {address}")
            print(f"       (top candidate was: {results[0].get('display_name')})")
            return None

        # No postcode to validate against - accept the top result but flag it
        verified = results[0].get("display_name")
        print(f"    ⚠️  Verified address (no postcode to cross-check, please review): {verified}")
        return verified

    except Exception as e:
        print(f"    ⚠️  Address lookup failed: {e}")
        return None


# ─────────────────────────────────────────────
# CLAUDE RESEARCH
# ─────────────────────────────────────────────

def research_club_with_claude(club_name):
    print(f"\n🤖 Asking Claude (Haiku) to research: {club_name}")

    # Check if manual override exists
    if MANUAL_OVERRIDE:
        print(f"  ✅ Using manual override for: {club_name}")
        event = dict(MANUAL_OVERRIDE)  # copy so we don't mutate the constant
        return event

    system_prompt = """You are an assistant that researches community groups and generates event details for the ActiveLocals platform.

When given a club name, use your knowledge to find information about that club, then return a JSON object with these exact fields:
{
  "title": "Event title (e.g. 'Chilli Chicks Run Club, Wednesday Morning Run')",
  "description": "2-3 sentence description of the club and what the event is",
  "what_to_expect": "2-3 sentences on what attendees can expect at the session",
  "website": "Club website or social media URL, or empty string if none found",
  "intensity": "One of: Just for Fun | Fit & Focused | High Performance",
  "address": "Full street address of where the event is held. Must be a real, searchable address.",
  "day_of_week": "The day of the week this recurring event happens, e.g. 'Wednesday'. If you are not confident, return an empty string.",
  "start_time": "Start time in 24h HH:MM local time at the venue, e.g. '06:30'. If not confident, return an empty string.",
  "end_time": "End time in 24h HH:MM local time at the venue, e.g. '07:30'. If not confident, return an empty string.",
  "recurring": true,
  "image_search_query": "The exact club name - this will be used to search Google Images for photos of this specific group."
}

Rules:
- No em dashes anywhere
- Keep descriptions factual and friendly
- If you are not confident about the day_of_week or times, return empty strings for those fields rather than guessing - the tool will prompt a human to fill them in
- For ADDRESS: Include a specific street number and name, not just a park name or suburb
- For image_search_query: Return the EXACT CLUB NAME from search results, not a generic query
- Return ONLY the JSON object, no other text, no markdown code blocks"""

    user_prompt = f"Research this club and generate event details: {club_name}"

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            },
            timeout=60
        )

        data = response.json()

        # Extract text from response
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        if not text:
            raise ValueError("No text content in Claude response")

        # Parse JSON from response
        text = text.strip()
        # Strip markdown code fences if present
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'^```\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        event = json.loads(text)
        print(f"  ✅ Claude returned event details for: {event.get('title')}")
        return event

    except Exception as e:
        print(f"  ❌ Claude research failed: {e}")
        print(f"     Response: {response.text[:500] if 'response' in locals() else 'no response'}")
        print(f"  ⚠️  Using skeleton event - fill in details manually via the context prompt")
        return {
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
        }


# ─────────────────────────────────────────────
# IMAGE DOWNLOAD
# ─────────────────────────────────────────────

def download_page_cover_image(cover_url, save_dir, filename):
    """Download the club's existing cover image directly from the CDN URL."""
    from PIL import Image
    import io as _io

    if not cover_url:
        return None

    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(cover_url, headers=headers, timeout=10)
        if resp.status_code != 200 or len(resp.content) < 8000:
            return None
        img = Image.open(_io.BytesIO(resp.content))
        img.load()
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.save(save_path, "JPEG", quality=90)
        print(f"  ✅ Downloaded cover image from page: {filename}")
        return save_path
    except Exception as e:
        print(f"  ⚠️  Could not download page cover image: {e}")
        return None



def download_image(club_name, query, save_dir, filename, location=None):
    """Download image for the specific club using DuckDuckGo image search.
    location: short string like 'Bronte NSW' used to disambiguate results."""
    from ddgs import DDGS
    from PIL import Image
    import io

    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    if os.path.exists(save_path):
        print(f"  📷 Using cached image: {filename}")
        return save_path

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # Build search queries: location-specific first (most accurate), then
    # progressively broader fallbacks
    loc = location.strip() if location else ""
    search_queries = []
    if loc:
        search_queries += [
            f"{club_name} {loc}",           # "Bronte Morning Move Breathe And Swim Bronte NSW"
            f"{club_name} {loc} group",
        ]
    search_queries += [
        club_name,
        f"{club_name} Australia",
        f"{club_name} group",
    ]
    # Domains that tend to return irrelevant/foreign stock images - skip these
    BLOCKED_DOMAINS = [
        "shutterstock", "gettyimages", "istockphoto", "dreamstime", "alamy",
        "123rf", "depositphotos", "stock.adobe", "bigstockphoto", "canstockphoto",
        # Russian/non-English sites that keep showing up
        "vk.com", "ok.ru", "mail.ru", ".ru/", "yandex", "rambler",
        # Generic clip art / icon sites
        "clipart", "flaticon", "freepik", "vectorstock", "pngtree",
    ]

    def is_blocked_url(url):
        url_lower = url.lower()
        return any(d in url_lower for d in BLOCKED_DOMAINS)

    # Preferred domains - results from these are trusted first
    PREFERRED_DOMAINS = [
        "instagram.com", "facebook.com", "strava.com",
        "meetup.com", "eventbrite.com", "activelocals",
        ".com.au", ".org.au", ".net.au",
    ]

    def is_preferred_url(url):
        url_lower = url.lower()
        return any(d in url_lower for d in PREFERRED_DOMAINS)

    # Deduplicate while preserving order
    seen = set()
    search_queries = [q for q in search_queries if not (q in seen or seen.add(q))]

    def try_download_url(url):
        """Download and validate a single image URL. Returns save_path or None."""
        try:
            img_resp = requests.get(url, headers=headers, timeout=8)
            if img_resp.status_code != 200 or len(img_resp.content) <= 8000:
                return None
            img = Image.open(io.BytesIO(img_resp.content))
            img.load()
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.save(save_path, "JPEG", quality=90)
            return save_path
        except Exception:
            return None

    for search_query in search_queries:
        print(f"  🔍 Searching images for: {search_query}")
        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(search_query, max_results=15))

            # Split into preferred and acceptable buckets, skip blocked entirely
            preferred = [r for r in results if not is_blocked_url(r.get("image","")) and is_preferred_url(r.get("image",""))]
            acceptable = [r for r in results if not is_blocked_url(r.get("image","")) and not is_preferred_url(r.get("image",""))]

            for result in preferred + acceptable:
                url = result.get("image")
                if not url or is_blocked_url(url):
                    continue
                path = try_download_url(url)
                if path:
                    print(f"  ✅ Image downloaded: {filename}  (from {url[:60]}...)")
                    return path

        except Exception as e:
            print(f"    ⚠️  Search failed for '{search_query}': {e}")

    print(f"  ⚠️  Could not download a valid image, you will need to upload one manually")
    return None


# ─────────────────────────────────────────────
# ADDRESS AUTOCOMPLETE HELPER
# ─────────────────────────────────────────────

def extract_meeting_point(text, state=None):
    """
    Searches scraped description text for a specific venue or meeting point
    (e.g. 'North Bondi Surf Club', 'Davies Park', 'Centennial Park').
    Returns a geocodable address string if found, else None.
    """
    if not text:
        return None

    # Patterns that indicate a specific meeting location
    VENUE_PATTERNS = [
        r'meet(?:ing)? at ([A-Z][A-Za-z\s]+(?:Park|Reserve|Beach|Oval|Club|Hall|Centre|Center|Pavilion|Pool|Wharf|Jetty|Point|Headland|Lagoon|Lake|River|Creek|Bay|Cove|Oval|Ground|Courts?))',
        r'start(?:ing)? at ([A-Z][A-Za-z\s]+(?:Park|Reserve|Beach|Oval|Club|Hall|Centre|Center|Pavilion|Pool|Wharf|Jetty|Point|Headland|Lagoon|Lake|River|Creek|Bay|Cove|Oval|Ground|Courts?))',
        r'gather(?:ing)? at ([A-Z][A-Za-z\s]+(?:Park|Reserve|Beach|Oval|Club|Hall|Centre|Center|Pavilion|Pool|Wharf|Jetty|Point|Headland|Lagoon|Lake|River|Creek|Bay|Cove|Oval|Ground|Courts?))',
        r'\bat ([A-Z][A-Za-z\s]+(?:Park|Reserve|Beach|Oval|Club|Hall|Centre|Center|Pavilion|Pool|Wharf|Jetty|Point|Headland|Lagoon|Lake|River|Creek|Bay|Cove|Oval|Ground|Courts?))',
        r'([A-Z][A-Za-z\s]+(?:Surf Club|SLSC|Swimming Club|Aquatic Centre|Leisure Centre))',
        r'([A-Z][A-Za-z\s]+(?:Park|Reserve|Beach|Oval))',
    ]

    for pattern in VENUE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            venue = match.group(1).strip()
            # Append state to make it geocodable
            query = f"{venue}, {state}, Australia" if state else f"{venue}, Australia"
            # Quick sanity check: venue name shouldn't be too short or generic
            if len(venue) > 5 and venue.lower() not in ("the park", "the beach", "the oval"):
                return query

    return None



    """
    Searches via Nominatim to get a clean short street address suitable for
    typing into an autocomplete field.
    If expected_state is provided (e.g. "NSW"), rejects results from a different
    state so a wrong-suburb collision can't silently pass.
    Returns a short address string or None.
    """
    if not query:
        return None

    headers = {"User-Agent": "ActiveLocalsEventUploader/1.0 (internal tool)"}

    STATE_FULL = {
        "NSW": "new south wales", "VIC": "victoria", "QLD": "queensland",
        "SA": "south australia", "WA": "western australia", "TAS": "tasmania",
        "NT": "northern territory", "ACT": "australian capital territory",
    }
    STATE_SHORT = {v: k for k, v in STATE_FULL.items()}
    SHORT_MAP = {
        "New South Wales": "NSW", "Victoria": "VIC", "Queensland": "QLD",
        "South Australia": "SA", "Western Australia": "WA",
        "Tasmania": "TAS", "Northern Territory": "NT",
        "Australian Capital Territory": "ACT"
    }

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "countrycodes": "au", "limit": 5, "addressdetails": 1},
            headers=headers,
            timeout=10,
        )
        results = resp.json()
        if not results:
            return None

        for r in results:
            addr = r.get("address", {})
            road = addr.get("road") or addr.get("pedestrian") or addr.get("path") or addr.get("footway")
            suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("city_district") or addr.get("town") or addr.get("city")
            state_full = addr.get("state", "")
            state_short = SHORT_MAP.get(state_full, state_full)
            postcode = addr.get("postcode", "")

            # Validate against expected state from page if provided
            if expected_state and state_short and state_short != expected_state:
                print(f"    ⚠️  Skipping result in {state_short} (expected {expected_state}): {r.get('display_name', '')[:60]}")
                continue

            if road and suburb:
                short_addr = f"{road}, {suburb} {state_short} {postcode}".strip()
                print(f"    ✅ Address found: {short_addr}")
                return short_addr

        # If nothing matched the state filter, warn and use first result anyway
        if expected_state:
            print(f"    ⚠️  No result matched state {expected_state}, using best available")
        display = results[0].get("display_name", "")
        parts = [p.strip() for p in display.split(",")]
        short_addr = ", ".join(parts[:4])
        print(f"    ⚠️  Using trimmed address: {short_addr}")
        return short_addr

    except Exception as e:
        print(f"    ⚠️  Address lookup failed: {e}")
        return None


def fill_address_with_autocomplete(scope, address, expected_state=None):
    """
    Looks up a clean street address via Nominatim/Google Maps style geocoding,
    types just enough of it to trigger the autocomplete, then waits for and
    clicks the first suggestion.

    `scope` is the dialog locator; autocomplete options are searched page-wide
    since MUI renders them in a portal outside the dialog DOM subtree.
    """
    page = getattr(scope, "page", scope)

    print(f"  📍 Looking up address: {address}")
    try:
        street_address = get_street_address_from_google_maps(address, expected_state=expected_state)
    except Exception:
        street_address = None

    if not street_address:
        # Geocoding failed or unavailable — type the raw address directly
        print(f"  ⚠️  Could not geocode '{address}' - typing it directly")
        street_address = address

    # Use just the first part (road + suburb) to trigger autocomplete
    # rather than pasting the full long string
    search_query = ", ".join(street_address.split(",")[:2]).strip()
    print(f"  📍 Typing into address field: {search_query}")

    loc_input = scope.get_by_label("Location", exact=False)
    loc_input.wait_for(state="visible", timeout=5000)
    loc_input.click()
    loc_input.fill("")
    time.sleep(0.3)

    # Type slowly so autocomplete triggers
    loc_input.type(search_query, delay=60)

    # Wait up to 5s for autocomplete options to appear
    autocomplete_selectors = [
        ".MuiAutocomplete-option",
        "li[role='option']",
        "[role='option']",
        ".pac-item",
    ]

    option_found = False
    for wait_sec in [2, 3, 4]:
        time.sleep(wait_sec - (0 if wait_sec == 2 else wait_sec - 1))
        for sel in autocomplete_selectors:
            try:
                first = page.locator(sel).first
                first.wait_for(state="visible", timeout=1500)
                first.click()
                print(f"  ✅ Selected autocomplete suggestion")
                time.sleep(0.5)
                option_found = True
                break
            except PlaywrightTimeout:
                continue
        if option_found:
            break

    if not option_found:
        # Nothing appeared — clear and try typing the suburb + state only (shorter = more likely to match)
        fallback = ", ".join(street_address.split(",")[1:3]).strip()
        if fallback and fallback != search_query:
            print(f"  🔄 No autocomplete, retrying with: {fallback}")
            loc_input.fill("")
            time.sleep(0.3)
            loc_input.type(fallback, delay=60)
            time.sleep(3)
            for sel in autocomplete_selectors:
                try:
                    first = page.locator(sel).first
                    first.wait_for(state="visible", timeout=2000)
                    first.click()
                    print(f"  ✅ Selected autocomplete suggestion (fallback)")
                    time.sleep(0.5)
                    option_found = True
                    break
                except PlaywrightTimeout:
                    continue

    if not option_found:
        print(f"  ⚠️  No autocomplete appeared - address left as typed, please check in the browser")

    return option_found


# ─────────────────────────────────────────────
# BROWSER: LOGIN
# ─────────────────────────────────────────────

def ensure_logged_in(page):
    """Navigates to /admin, logs in automatically if ACTIVELOCALS_EMAIL/PASSWORD are set,
    otherwise waits for manual login. Returns True on success."""
    print("\n🔐 Checking login status...")
    page.goto(f"{BASE_URL}/admin", wait_until="networkidle", timeout=20000)
    time.sleep(2)

    current_url = page.url
    print(f"  Current URL: {current_url}")

    if "/login" not in current_url:
        print("  ✅ Already authenticated")
        return True

    print("  🔐 Login page detected")

    if EMAIL and PASSWORD:
        print("  🤖 Attempting automatic login...")
        try:
            email_field = page.locator(
                "input[type='email'], input[name='email'], input#email"
            ).first
            email_field.wait_for(state="visible", timeout=8000)
            email_field.click()
            email_field.fill(EMAIL)

            password_field = page.locator(
                "input[type='password'], input[name='password'], input#password"
            ).first
            password_field.wait_for(state="visible", timeout=5000)
            password_field.click()
            password_field.fill(PASSWORD)

            submit_button = page.locator(
                "button[type='submit'], "
                "button:has-text('Sign in'), button:has-text('Log in'), button:has-text('Continue')"
            ).first
            submit_button.click()
            print("  ✅ Submitted login form, waiting for redirect...")
            time.sleep(3)
        except Exception as e:
            print(f"  ⚠️  Automatic login failed ({e}) - falling back to manual login")
    else:
        print("  ⚠️  ACTIVELOCALS_EMAIL/ACTIVELOCALS_PASSWORD not set - falling back to manual login")

    print("  📝 If the browser isn't logged in yet, please log in manually")
    print("  ⏳ Waiting for login to complete...")
    print("  (Script will continue automatically when you're logged in)")

    max_wait = 300
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(1)
        elapsed += 1

        # Click "Sign in as" confirmation button if it appears
        try:
            signin_button = page.locator("button:has-text('Sign in as')")
            if signin_button.is_visible():
                print("  🖱️  Clicking 'Sign in as' button...")
                signin_button.click()
                time.sleep(2)
        except Exception:
            pass

        if page.url.startswith("https://mapqhipy2m.ap-southeast-2.awsapprunner.com"):
            print(f"  ✅ Login successful! ({page.url[:60]})")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            time.sleep(2)
            return True

        if elapsed % 10 == 0:
            print(f"  ⏳ Still waiting... ({elapsed}s) URL: {page.url[:80]}")

    print("  ❌ Login timeout")
    return False


# ─────────────────────────────────────────────
# BROWSER: NAVIGATE TO CLUB
# ─────────────────────────────────────────────

def scrape_club_page(page):
    """
    Scrapes the club page and returns a dict with:
      - description_text: full page text for schedule extraction
      - page_location: location string from sidebar e.g. "North Bondi, NSW, 2026"
      - page_state: AU state code e.g. "NSW"
      - cover_image_url: CDN URL of the club cover photo if present
    """
    result = {"description_text": "", "page_location": "", "page_state": "", "cover_image_url": ""}

    try:
        # ── DESCRIPTION TEXT (for schedule extraction) ──
        all_text = []
        for sel in ["main p", ".MuiTypography-body1"]:
            els = page.locator(sel).all()
            for el in els:
                try:
                    txt = el.inner_text().strip()
                    if txt and len(txt) > 20:
                        all_text.append(txt)
                except Exception:
                    continue
            if all_text:
                break
        result["description_text"] = "\n".join(all_text)

        # ── COVER IMAGE ──
        try:
            for img in page.locator("main img").all():
                src = img.get_attribute("src") or ""
                if src.startswith("http") and "cloudfront" in src:
                    result["cover_image_url"] = src
                    print(f"  🖼️  Found page cover image")
                    break
        except Exception:
            pass

        # ── LOCATION (3 fallback strategies) ──
        location_text = ""

        # Strategy 1: h6 "Location" then next sibling p
        if not location_text:
            try:
                heading = page.locator("h6:has-text('Location')").first
                location_text = heading.locator("xpath=following-sibling::p[1]").inner_text(timeout=2000).strip()
            except Exception:
                pass

        # Strategy 2: LocationOnIcon svg — go up to parent Box then find p
        if not location_text:
            try:
                icon = page.locator("[data-testid='LocationOnIcon']").first
                parent = icon.locator("xpath=../..")
                location_text = parent.locator("p").first.inner_text(timeout=2000).strip()
            except Exception:
                pass

        # Strategy 3: scan all <p> tags for one containing an AU state code
        if not location_text:
            try:
                for el in page.locator("main p").all():
                    try:
                        txt = el.inner_text().strip()
                        if re.search(r'\b(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\b', txt) and len(txt) < 120:
                            location_text = txt
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if location_text:
            result["page_location"] = location_text
            print(f"  📍 Page location: {location_text}")
        else:
            print(f"  ⚠️  Could not extract page location")

        # ── STATE ──
        for state in ["NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"]:
            if state in result["page_location"].upper():
                result["page_state"] = state
                break

    except Exception as e:
        print(f"  ⚠️  Could not scrape club page: {e}")

    return result


def extract_schedule_from_text(text):
    """
    Tries to extract day-of-week and time from text like:
      "See you Every Wednesday! 🕐 1:00-2:00 PM"
      "every Monday at 6:30am"
      "Tuesdays 10:00"
      "Wed 7pm"
    Returns: (day_of_week, start_time_24h, end_time_24h) or (None, None, None).
    """
    if not text:
        return None, None, None

    text_lower = text.lower()

    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    found_day = None
    for day in days:
        if day in text_lower:
            found_day = day.capitalize()
            break

    if not found_day:
        return None, None, None

    # Match time range like "1:00-2:00 PM" or "6:30am-7:30am" or just "1:00 PM"
    time_range = re.search(
        r'(\d{1,2}):?(\d{2})?\s*(am|pm)?'
        r'(?:\s*[-–to]+\s*(\d{1,2}):?(\d{2})?\s*(am|pm)?)?',
        text_lower
    )

    if not time_range:
        return found_day, None, None

    def to_24h(h, m, meridiem):
        h, m = int(h), int(m or 0)
        # Sanity check — invalid times should be rejected
        if h > 23 or m > 59:
            return None
        if meridiem:
            if 'p' in meridiem and h != 12:
                h += 12
            elif 'a' in meridiem and h == 12:
                h = 0
        if h > 23:
            return None
        return f"{h:02d}:{m:02d}"

    start_time = to_24h(
        time_range.group(1),
        time_range.group(2),
        time_range.group(3)
    )

    if not start_time:
        return found_day, None, None

    end_time = None
    if time_range.group(4):
        # If end time has no meridiem but start does, inherit it
        end_meridiem = time_range.group(6) or time_range.group(3)
        end_time = to_24h(
            time_range.group(4),
            time_range.group(5),
            end_meridiem
        )

    return found_day, start_time, end_time


def navigate_to_club(page, club_id):
    """
    Navigates to a club's admin page, waits for the Create Event button,
    scrapes the description text (which often contains schedule info),
    and returns (True, scraped_text) on success or (False, "") on failure.
    """
    club_url = f"{BASE_URL}/admin/clubs/{club_id}"
    print(f"\n🌐 Navigating to: {club_url}")

    max_nav_attempts = 3
    for attempt in range(1, max_nav_attempts + 1):
        try:
            page.goto(club_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_selector("button:has-text('Create Event')", timeout=15000)

            # Scrape description text and sidebar location from the loaded page
            scraped = scrape_club_page(page)
            if scraped["description_text"]:
                print(f"  📄 Scraped page text ({len(scraped['description_text'])} chars)")
            return True, scraped

        except PlaywrightTimeout:
            print(f"  ⚠️  Navigation attempt {attempt}/{max_nav_attempts} timed out (currently at {page.url}), retrying...")
            time.sleep(2)
        except Exception as e:
            err = str(e)
            if "ERR_ABORTED" in err or "net::" in err:
                # SPA intercepted the navigation - wait for it to settle then check if we're there
                print(f"  ⚠️  Navigation aborted (SPA redirect), waiting for page to settle...")
                time.sleep(3)
                try:
                    page.wait_for_selector("button:has-text('Create Event')", timeout=10000)
                    scraped = scrape_club_page(page)
                    if scraped["description_text"]:
                        print(f"  📄 Scraped page text ({len(scraped['description_text'])} chars)")
                    return True, scraped
                except PlaywrightTimeout:
                    print(f"  ⚠️  Still no Create Event button after ERR_ABORTED, retrying goto...")
                    time.sleep(2)
            else:
                print(f"  ⚠️  Navigation error on attempt {attempt}: {err[:120]}")
                time.sleep(2)

    print("  ❌ Could not reach club page after multiple attempts.")
    return False, ""


# ─────────────────────────────────────────────
# BROWSER: FILL AND SUBMIT THE EVENT FORM
# (extracted so it can be reused for one club or looped over many)
# ─────────────────────────────────────────────

def create_event_for_club(page, event, image_path, pause_for_review=True, page_state=None):
    """
    Assumes page is already navigated to the club's admin page (navigate_to_club already called).
    Clicks Create Event and fills out the whole form. Returns True if submitted successfully.

    NOTE: Deliberately uses label-based locators scoped to the dialog rather than
    the auto-generated :rX: element ids - those ids come from React's useId and
    shift depending on what else has rendered on the page, so hardcoding them
    is unreliable (they sometimes point at completely different elements).
    """
    intensity_raw = event.get("intensity", "Just for Fun")
    intensity_label = INTENSITY_MAP.get(intensity_raw.lower(), intensity_raw)

    # ── CLICK CREATE EVENT ──
    print("  🖱️  Clicking Create Event...")
    page.click("button:has-text('Create Event')")
    time.sleep(2)

    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible", timeout=10000)

    # ── UPLOAD IMAGE ──
    if image_path:
        print("  📷 Uploading image...")
        dialog.locator("input[type='file'][accept*='image']").set_input_files(image_path)
        time.sleep(1)
    else:
        print("  ⚠️  No image available, upload manually")

    # ── EVENT TITLE ──
    print("  ✏️  Filling Event Title...")
    title_input = dialog.get_by_label("Event Title", exact=False)
    title_input.click()
    title_input.fill(event["title"])

    # ── DESCRIPTION ──
    print("  ✏️  Filling Description...")
    desc_input = dialog.get_by_label("Description", exact=True)
    desc_input.click()
    desc_input.fill(event["description"])

    # ── WHAT TO EXPECT ──
    print("  ✏️  Filling What to Expect...")
    expect_input = dialog.get_by_label("What to expect", exact=False)
    expect_input.click()
    expect_input.fill(event["what_to_expect"])

    # ── WEBSITE ──
    print("  ✏️  Filling Website...")
    website_input = dialog.get_by_label("Website", exact=False)
    website_input.click()
    website_input.fill(event.get("website", ""))

    # ── INTENSITY ──
    print(f"  ✏️  Setting Intensity to: {intensity_label}")
    dialog.locator(".MuiSelect-select").click()
    time.sleep(1)
    page.wait_for_selector("li[role='option']", state="visible", timeout=5000)
    page.click(f"li[role='option']:has-text('{intensity_label}')", timeout=5000)
    time.sleep(0.5)

    # ── LOCATION (with autocomplete) ──
    fill_address_with_autocomplete(dialog, event["address"], expected_state=page_state)

    # ── START DATE & TIME ──
    print("  ✏️  Filling Start Date & Time...")
    start_input = dialog.get_by_label("Start Date", exact=False)
    if event.get("start_dt"):
        start_input.fill(event["start_dt"])
    else:
        print("     (left blank - please pick manually in the browser)")

    # ── END DATE & TIME ──
    print("  ✏️  Filling End Date & Time...")
    end_input = dialog.get_by_label("End Date", exact=False)
    if event.get("end_dt"):
        end_input.fill(event["end_dt"])
    else:
        print("     (left blank - please pick manually in the browser)")

    # ── RECURRENCE ──
    if event.get("recurring"):
        print("  🔁 Enabling Recurrence...")
        dialog.locator("h6:has-text('Recurrence')").scroll_into_view_if_needed()
        time.sleep(0.5)
        # Scope to the dialog so we never accidentally hit the page-level
        # "Show admin tags" switch, which also matches a generic checkbox selector.
        recurrence_checkbox = dialog.get_by_label("Recurring event", exact=False)
        recurrence_checkbox.click(force=True)
        time.sleep(0.5)
        print("  ✅ Recurrence enabled")

    # ── PAUSE FOR REVIEW ──
    if pause_for_review:
        print("\n👀 Form filled. Please review in the browser.")
        print("─" * 60)
        print("  Optional: paste extra context to fix missing fields.")
        print("  Examples: 'every Monday at 5:15am at Olivers Hill Frankston'")
        print("            'at Centennial Park NSW'")
        print("  Leave blank and press Enter to move to the next club.")
        print("─" * 60)
        extra_context = input("  → ").strip()

        if extra_context:
            print("  🔄 Applying extra context...")

            # Fix missing date/time from context
            if not event.get("start_dt"):
                day, start_t, end_t = extract_schedule_from_text(extra_context)
                if day:
                    event["day_of_week"] = day
                    if start_t:
                        event["start_time"] = start_t
                    if end_t:
                        event["end_time"] = end_t
                    elif start_t:
                        h, m = map(int, start_t.split(":"))
                        event["end_time"] = f"{(h+1)%24:02d}:{m:02d}"
                    event.pop("start_dt", None)
                    event.pop("end_dt", None)
                    event = resolve_event_datetimes(event, interactive=False)
                    if event.get("start_dt"):
                        print(f"  📅 Updated dates: {event['start_dt']} to {event['end_dt']}")
                        try:
                            dialog.get_by_label("Start Date", exact=False).fill(event["start_dt"])
                            dialog.get_by_label("End Date", exact=False).fill(event["end_dt"])
                        except Exception:
                            pass

            # Fix missing address from context — match "at/in/near [Proper Noun venue]"
            # Use word boundary and limit match length to avoid grabbing sentence fragments
            addr_match = re.search(
                r'(?:^|\s)(?:at|in|near)\s+([A-Z][A-Za-z0-9\']+(?:\s+[A-Z][A-Za-z0-9\']+){0,6}'
                r'(?:\s*,\s*\d+\s+[A-Za-z\s]+)?'
                r'(?:\s*,\s*(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT))?'
                r'(?:\s+\d{4})?)',
                extra_context, re.IGNORECASE
            )
            if addr_match:
                new_addr = addr_match.group(1).strip().rstrip(",")
                print(f"  📍 Extracted address from context: {new_addr}")
                fill_address_with_autocomplete(dialog, new_addr, expected_state=page_state)

        print("─" * 60)
        input("  → Press Enter to move to the next club: ")

    return True


# ─────────────────────────────────────────────
# MAIN AUTOMATION (single club, run directly)
# ─────────────────────────────────────────────

def run():
    # 1. Research club with Claude
    event = research_club_with_claude(CLUB_NAME)
    if not event:
        print("\n❌ Could not get event details from Claude. Exiting.")
        return

    # 1b. Resolve start/end datetimes (next occurrence, or prompt if unknown)
    event = resolve_event_datetimes(event)

    # Print what Claude found for review
    print("\n📋 Event details from Claude:")
    for k, v in event.items():
        print(f"   {k}: {v}")

    confirm = input("\n✅ Looks good? Press Enter to continue, or Ctrl+C to cancel: ")

    # 2. Download image
    image_query = event.get("image_search_query", f"{CLUB_NAME} Australia")
    image_path = download_image(
        club_name=CLUB_NAME,
        query=image_query,
        save_dir=IMAGE_DIR,
        filename=f"{CLUB_ID}.jpg"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=60, args=["--start-maximized"])
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        if not ensure_logged_in(page):
            return

        ok, _ = navigate_to_club(page, CLUB_ID)
        if not ok:
            return

        create_event_for_club(page, event, image_path, pause_for_review=True)

        print("\n🎉 Done! Press Enter to close the browser...")
        input()
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research and prefill an ActiveLocals event.")
    parser.add_argument("--club-id", help="Club UUID (overrides CLUB_ID constant)")
    parser.add_argument("--club-name", help="Club name (overrides CLUB_NAME constant)")
    args = parser.parse_args()

    if args.club_id and args.club_name:
        CLUB_ID = args.club_id
        CLUB_NAME = args.club_name
        # A specific club was requested on the command line, so ignore the
        # MANUAL_OVERRIDE test fixture above and do live Claude research instead.
        MANUAL_OVERRIDE = None
    elif args.club_id or args.club_name:
        parser.error("--club-id and --club-name must be provided together")

    if not CLUB_ID or not CLUB_NAME:
        parser.error(
            "No club specified. Pass --club-id and --club-name, "
            "or run batch_create_events.py to process every unprocessed row in the sheet."
        )

    run()
