import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ORGANIZER_ID = os.environ.get("SHOTGUN_ORGANIZER_ID", "").strip()
TOKEN = os.environ.get("SHOTGUN_API_TOKEN", "").strip()

if not ORGANIZER_ID or not TOKEN:
    print("Missing SHOTGUN_ORGANIZER_ID or SHOTGUN_API_TOKEN", file=sys.stderr)
    sys.exit(1)

API_URL = f"https://smartboard-api.shotgun.live/api/shotgun/organizers/{ORGANIZER_ID}/events"
PORTRAIT_OVERRIDES_PATH = Path("shotgun-portrait-overrides.json")


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


def load_portrait_overrides():
    if not PORTRAIT_OVERRIDES_PATH.exists():
        return {}

    try:
        data = json.loads(PORTRAIT_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read {PORTRAIT_OVERRIDES_PATH}: {type(exc).__name__}", file=sys.stderr)
        return {}

    return data if isinstance(data, dict) else {}


def api_event_to_public(event, portrait_overrides):
    slug = event.get("slug")
    event_id = event.get("id")
    url = event.get("url") or (f"https://shotgun.live/events/{slug}" if slug else None)
    organizer = event.get("organizer") or {}
    location = event.get("location") or event.get("venue") or {}

    portrait_url = None
    if slug and slug in portrait_overrides:
        portrait_url = portrait_overrides[slug]
    elif event_id is not None and str(event_id) in portrait_overrides:
        portrait_url = portrait_overrides[str(event_id)]

    return {
        "id": event_id,
        "name": event.get("name"),
        "start_time": event.get("startTime"),
        "end_time": event.get("endTime"),
        "url": url,
        "slug": slug,
        "cover_url": event.get("coverUrl") or event.get("coverThumbnailUrl"),
        "portrait_url": portrait_url,
        "description": event.get("description"),
        "visibility": event.get("visibility"),
        "organizer_name": organizer.get("name") if isinstance(organizer, dict) else None,
        "location_name": location.get("name") if isinstance(location, dict) else None,
    }


portrait_overrides = load_portrait_overrides()

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

    item = api_event_to_public(event, portrait_overrides)
    if item["name"] and item["start_time"]:
        events.append(item)

events.sort(
    key=lambda event: parse_dt(event.get("start_time"))
    or datetime.max.replace(tzinfo=timezone.utc)
)

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

print(
    f"Wrote {len(events)} current/future Shotgun event(s) to events.json "
    f"with {sum(1 for event in events if event.get('portrait_url'))} portrait override(s)"
)
