import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ORGANIZER_ID = os.environ.get("SHOTGUN_ORGANIZER_ID", "").strip()
TOKEN = os.environ.get("SHOTGUN_API_TOKEN", "").strip()

if not ORGANIZER_ID or not TOKEN:
    print("Missing SHOTGUN_ORGANIZER_ID or SHOTGUN_API_TOKEN", file=sys.stderr)
    sys.exit(1)

API_URL = f"https://smartboard-api.shotgun.live/api/shotgun/organizers/{ORGANIZER_ID}/events"
PARIS_TZ = ZoneInfo("Europe/Paris")
VISIBLE_WEEKDAYS = {3, 4, 5}  # Thursday, Friday, Saturday (Python Monday=0)
PORTRAIT_RATIO_MIN = 0.64
PORTRAIT_RATIO_MAX = 0.86


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def event_end_from_api(event):
    end = parse_dt(event.get("endTime"))
    if end:
        return end
    start = parse_dt(event.get("startTime"))
    return start + timedelta(hours=8) if start else None


def event_is_current_or_future(event, now):
    end = event_end_from_api(event)
    return bool(end and end > now)


def api_event_to_public(event):
    slug = event.get("slug")
    url = event.get("url") or (f"https://shotgun.live/events/{slug}" if slug else None)
    organizer = event.get("organizer") or {}
    location = event.get("location") or event.get("venue") or {}

    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "start_time": event.get("startTime"),
        "end_time": event.get("endTime"),
        "url": url,
        "slug": slug,
        # The organizer API exposes the 16:9 banner here.
        "cover_url": event.get("coverUrl") or event.get("coverThumbnailUrl"),
        # Filled below from the rendered public Shotgun page when available.
        "portrait_url": None,
        "description": event.get("description"),
        "visibility": event.get("visibility"),
        "organizer_name": organizer.get("name") if isinstance(organizer, dict) else None,
        "location_name": location.get("name") if isinstance(location, dict) else None,
    }


def select_visible_events(events, now):
    """Pick the event currently occupying each Thu/Fri/Sat homepage slot.

    An event stays selected until its end_time. This prevents the site from
    replacing tonight's event with next week's event before the night is over.
    """
    visible = {}
    for event in events:
        start = parse_dt(event.get("start_time"))
        if not start:
            continue
        end = parse_dt(event.get("end_time")) or (start + timedelta(hours=8))
        if end <= now:
            continue
        weekday = start.astimezone(PARIS_TZ).weekday()
        if weekday not in VISIBLE_WEEKDAYS:
            continue
        if weekday not in visible:
            visible[weekday] = event
    return visible


def normalise_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return None


def event_name_tokens(name):
    return {
        token.lower()
        for token in re.findall(r"[A-Za-zÀ-ÿ0-9@]+", name or "")
        if len(token) >= 4
    }


def choose_portrait_candidate(images, event_name):
    tokens = event_name_tokens(event_name)
    candidates = []

    for image in images:
        src = normalise_url(image.get("src"))
        try:
            width = int(image.get("width") or 0)
            height = int(image.get("height") or 0)
        except (TypeError, ValueError):
            continue

        if not src or width < 350 or height < 450:
            continue

        ratio = width / height if height else 0
        if not (PORTRAIT_RATIO_MIN <= ratio <= PORTRAIT_RATIO_MAX):
            continue

        lower_src = src.lower()
        alt = str(image.get("alt") or "").lower()

        # Avoid obvious non-event imagery.
        if any(word in lower_src for word in ("avatar", "logo", "icon", "profile")):
            continue

        score = width * height
        if "shotgun" in lower_src:
            score += 2_000_000
        if "/artworks/" in lower_src or "artwork" in lower_src:
            score += 4_000_000
        if any(token in alt for token in tokens):
            score += 2_000_000

        # Prefer a 4:5 / 3:4 artwork over a very tall story-like asset.
        score -= int(abs(ratio - 0.78) * 1_000_000)
        candidates.append((score, src, width, height, alt))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    _, src, width, height, _ = candidates[0]
    print(f"Selected portrait artwork {width}x{height}: {src.split('?')[0]}")
    return src


def fetch_portraits_for_visible_events(events, now):
    visible = list(select_visible_events(events, now).values())
    if not visible:
        return

    browser_path = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    if not browser_path:
        print("No Chrome/Chromium found; using Shotgun banner fallback", file=sys.stderr)
        return

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed; using Shotgun banner fallback", file=sys.stderr)
        return

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=browser_path,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1800},
            device_scale_factor=1,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
        )

        try:
            for event in visible:
                slug = event.get("slug")
                if not slug:
                    continue

                page = context.new_page()
                try:
                    url = f"https://shotgun.live/fr/events/{slug}"
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(3_000)

                    title = page.title()
                    images = page.evaluate(
                        """
                        () => Array.from(document.images).map((img) => ({
                          src: img.currentSrc || img.src || '',
                          width: img.naturalWidth || 0,
                          height: img.naturalHeight || 0,
                          alt: img.alt || ''
                        }))
                        """
                    )

                    event["portrait_url"] = choose_portrait_candidate(images, event.get("name"))
                    if event["portrait_url"]:
                        print(f"Portrait found for {slug} ({title})")
                    else:
                        print(
                            f"No vertical artwork found for {slug} ({title}); using banner fallback",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    print(
                        f"Could not render Shotgun page for {slug}: {type(exc).__name__}",
                        file=sys.stderr,
                    )
                finally:
                    page.close()
        finally:
            context.close()
            browser.close()


query = urllib.parse.urlencode({"key": TOKEN, "limit": 100})
request = urllib.request.Request(
    f"{API_URL}?{query}",
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "User-Agent": "danceteria-site-shotgun-sync/1.0",
    },
)

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
except urllib.error.HTTPError as exc:
    print(f"Shotgun API returned HTTP {exc.code}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"Shotgun API request failed: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)

raw_events = payload.get("data", []) if isinstance(payload, dict) else []
now = datetime.now(timezone.utc)

events = []
for event in raw_events:
    if not isinstance(event, dict):
        continue
    if event.get("cancelledAt") or not event.get("publishedAt"):
        continue
    if not event_is_current_or_future(event, now):
        continue
    item = api_event_to_public(event)
    if item["name"] and item["start_time"]:
        events.append(item)

events.sort(key=lambda event: parse_dt(event.get("start_time")) or datetime.max.replace(tzinfo=timezone.utc))

# The API gives us titles/dates/links reliably, but only the banner image.
# Render only the events that are actually visible on the homepage to recover
# Shotgun's vertical artwork without making the hourly job unnecessarily heavy.
fetch_portraits_for_visible_events(events, now)

output = {
    "source": "Shotgun Events API",
    "organizer_id": ORGANIZER_ID,
    "updated_at": now.isoformat(),
    "events": events,
}

Path("events.json").write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(f"Wrote {len(events)} current/future Shotgun event(s) to events.json")
