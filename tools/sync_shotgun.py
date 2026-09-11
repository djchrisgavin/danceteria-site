import json
import os
import re
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
    return bool(end and end >= now)


def fetch_jsonld_image(slug):
    if not slug:
        return None
    url = f"https://r.shotgun.live/fr/events/{slug}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            source = response.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        source,
        re.DOTALL | re.IGNORECASE,
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
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


def public_event(event, inspect=False):
    slug = event.get("slug")
    url = event.get("url") or (f"https://shotgun.live/events/{slug}" if slug else None)
    organizer = event.get("organizer") or {}
    location = event.get("location") or event.get("venue") or {}
    item = {
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
    if inspect:
        item["_jsonld_image_probe"] = fetch_jsonld_image(slug)
    return item


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
    item = public_event(event, inspect=len(events) == 0)
    if item["name"] and item["start_time"]:
        events.append(item)

events.sort(key=lambda event: parse_dt(event.get("start_time")) or datetime.max.replace(tzinfo=timezone.utc))

output = {
    "source": "Shotgun Events API",
    "organizer_id": ORGANIZER_ID,
    "updated_at": now.isoformat(),
    "events": events,
}

Path("events.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

index_path = Path("index.html")
index = index_path.read_text(encoding="utf-8")
if "shotgun-agenda.js" not in index:
    index = index.replace("\ninitialiseProgramme();\n", "\n// Agenda public piloté par Shotgun.\n// initialiseProgramme();\n", 1)
    index = index.replace("\n</body>", "\n<script src=\"shotgun-agenda.js?v=20260911-1\"></script>\n\n</body>", 1)
    index_path.write_text(index, encoding="utf-8")

print(f"Wrote {len(events)} current/future Shotgun event(s) to events.json")
