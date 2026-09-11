# Triggered after fixing the organizer ID in the workflow.
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
IMAGE_KEYWORDS = ("image", "cover", "artwork", "poster", "thumbnail", "portrait", "square", "vertical")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def event_is_current_or_future(event, now):
    end = parse_dt(event.get("endTime"))
    start = parse_dt(event.get("startTime"))
    if end:
        return end >= now
    if start:
        return start + timedelta(hours=8) >= now
    return False


def extract_image_fields(value, prefix="", depth=0):
    if depth > 4:
        return {}

    found = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            key_lower = str(key).lower()
            if any(word in key_lower for word in IMAGE_KEYWORDS):
                if isinstance(child, (str, int, float, bool)) or child is None:
                    found[path] = child
                elif isinstance(child, (list, dict)):
                    found[path] = child
            found.update(extract_image_fields(child, path, depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value[:10]):
            path = f"{prefix}[{index}]"
            found.update(extract_image_fields(child, path, depth + 1))
    return found


def public_event(event):
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
        "description": event.get("description"),
        "visibility": event.get("visibility"),
        "organizer_name": organizer.get("name") if isinstance(organizer, dict) else None,
        "location_name": location.get("name") if isinstance(location, dict) else None,
        "_image_fields": extract_image_fields(event),
    }


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
    if event.get("cancelledAt"):
        continue
    if not event.get("publishedAt"):
        continue
    if not event_is_current_or_future(event, now):
        continue
    item = public_event(event)
    if item["name"] and item["start_time"]:
        events.append(item)

events.sort(key=lambda event: parse_dt(event.get("start_time")) or datetime.max.replace(tzinfo=timezone.utc))

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

# One-time migration of the homepage agenda: stop the old Sheet renderer and
# load the public Shotgun renderer instead. The rest of the Sheet code stays
# in place so rollback is trivial.
index_path = Path("index.html")
index = index_path.read_text(encoding="utf-8")

if "shotgun-agenda.js" not in index:
    index = index.replace(
        "\ninitialiseProgramme();\n",
        "\n// Agenda public piloté par Shotgun.\n// initialiseProgramme();\n",
        1,
    )
    index = index.replace(
        "\n</body>",
        "\n<script src=\"shotgun-agenda.js?v=20260911-1\"></script>\n\n</body>",
        1,
    )
    index_path.write_text(index, encoding="utf-8")

print(f"Wrote {len(events)} current/future Shotgun event(s) to events.json")
