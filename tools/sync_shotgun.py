import json
import os
import re
import shutil
import subprocess
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


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def event_end(event):
    end = parse_dt(event.get("endTime"))
    if end:
        return end
    start = parse_dt(event.get("startTime"))
    return start + timedelta(hours=8) if start else None


def event_is_current_or_future(event, now):
    end = event_end(event)
    return bool(end and end > now)


def extract_jsonld_image(rendered_html):
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        rendered_html,
        re.DOTALL | re.IGNORECASE,
    )

    def candidates_from(data):
        if isinstance(data, list):
            for item in data:
                yield from candidates_from(item)
        elif isinstance(data, dict):
            yield data
            graph = data.get("@graph")
            if graph:
                yield from candidates_from(graph)

    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue

        for item in candidates_from(data):
            event_type = item.get("@type")
            if isinstance(event_type, list):
                is_event = any("Event" in str(value) for value in event_type)
            else:
                is_event = "Event" in str(event_type)
            if not is_event:
                continue

            image = item.get("image")
            if isinstance(image, str) and image:
                return image
            if isinstance(image, list) and image:
                first = image[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict):
                    return first.get("url") or first.get("contentUrl")
            if isinstance(image, dict):
                return image.get("url") or image.get("contentUrl")

    return None


def fetch_portrait_from_shotgun(slug):
    if not slug:
        return None

    browser = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    if not browser:
        print("No Chrome/Chromium found; using Shotgun banner fallback", file=sys.stderr)
        return None

    url = f"https://shotgun.live/fr/events/{slug}"
    command = [
        browser,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--virtual-time-budget=6000",
        "--dump-dom",
        url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except Exception as exc:
        print(f"Could not render Shotgun page for {slug}: {type(exc).__name__}", file=sys.stderr)
        return None

    if result.returncode != 0 or not result.stdout:
        print(f"Shotgun page render failed for {slug}", file=sys.stderr)
        return None

    image = extract_jsonld_image(result.stdout)
    if image:
        print(f"Portrait artwork found for {slug}")
    else:
        print(f"No JSON-LD artwork found for {slug}; using banner fallback", file=sys.stderr)
    return image


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
        "cover_url": event.get("coverUrl") or event.get("coverThumbnailUrl"),
        "portrait_url": None,
        "description": event.get("description"),
        "visibility": event.get("visibility"),
        "organizer_name": organizer.get("name") if isinstance(organizer, dict) else None,
        "location_name": location.get("name") if isinstance(location, dict) else None,
    }


def select_visible_events(events, now):
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

# Only fetch portrait artwork for the three events that can actually appear on
# the homepage. Future events stay in events.json so the browser can switch to
# them immediately after the previous event's end_time, even between sync runs.
for event in select_visible_events(events, now).values():
    event["portrait_url"] = fetch_portrait_from_shotgun(event.get("slug"))

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
