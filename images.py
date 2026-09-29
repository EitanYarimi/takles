#!/usr/bin/env python3
"""Pick a relevant, copyright-safe image per story.

Strategy (honest, no publisher photos):
  1. Detect the main entity (person / place / org / country) in the story.
  2. Fetch a freely-licensed lead photo of that entity from Wikimedia Commons,
     keeping the required attribution + license.
  3. Fall back to a public-domain national flag for country stories.
  4. Otherwise return nothing — the client shows its bundled category art.

Only Wikimedia Commons files under PD / CC0 / CC-BY / CC-BY-SA are accepted; local
non-free wiki uploads (logos, fair-use) are skipped. Results are cached by entity.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE_CACHE_PATH = ROOT / "image_cache.json"
IMAGE_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days
SSL_CTX = ssl._create_unverified_context()
UA = "ClearNewsPOC/0.4 (https://github.com/EitanYarimi/takles; images)"
HTTP_TIMEOUT = 12
THUMB_WIDTH = 900

# License strings we accept (free to reuse with attribution / public domain).
_LICENSE_OK = re.compile(
    r"public domain|^pd|cc0|cc[ \-]?by(?:[ \-]?sa)?", re.I
)

# Entity → Hebrew Wikipedia article title, ordered by priority (people first, then
# orgs/places, then countries). `cc` enables a public-domain flag fallback.
# Each: (compiled keyword regex, wiki title, kind, country-code-or-None)
_ENTITY_SPECS: list[tuple[str, str, str, str | None]] = [
    # People
    (r"יאיר נתניהו", "יאיר נתניהו", "person", None),
    (r"בנימין נתניהו|נתניהו|ביבי", "בנימין נתניהו", "person", None),
    (r"בן גביר", "איתמר בן גביר", "person", None),
    (r"סמוטריץ", "בצלאל סמוטריץ'", "person", None),
    (r"יאיר לפיד|לפיד", "יאיר לפיד", "person", None),
    (r"בני גנץ|גנץ", "בני גנץ", "person", None),
    (r"איזנקוט", "גדי איזנקוט", "person", None),
    (r"הרצוג", "יצחק הרצוג", "person", None),
    (r"דונלד טראמפ|טראמפ", "דונלד טראמפ", "person", None),
    (r"ביידן", "ג'ו ביידן", "person", None),
    (r"פוטין", "ולדימיר פוטין", "person", None),
    (r"זלנסקי", "וולודימיר זלנסקי", "person", None),
    (r"חמינאי|ח'אמנהאי", "עלי ח'אמנהאי", "person", None),
    # Orgs / institutions
    (r"הכנסת|כנסת", "הכנסת", "org", None),
    (r"צה\"?ל|צבא ההגנה", "צבא ההגנה לישראל", "org", None),
    (r"שב\"?כ|שירות הביטחון", "שירות הביטחון הכללי", "org", None),
    (r"חיזבאללה|חזבאללה", "חזבאללה", "org", None),
    (r"חמאס", "חמאס", "org", None),
    (r"החות'ים|חות'ים|ח'ותים", "חות'ים", "org", None),
    (r"נאט\"?ו", "נאט\"ו", "org", None),
    (r"פנטגון|הפנטגון", "הפנטגון", "org", None),
    (r"האיחוד האירופי", "האיחוד האירופי", "org", None),
    # Places
    (r"מצר הורמוז|הורמוז", "מצר הורמוז", "place", None),
    (r"רצועת עזה|עזה", "רצועת עזה", "place", None),
    (r"קפריסין", "קפריסין", "place", "cy"),
    (r"מדריד", "מדריד", "place", None),
    (r"הרצליה", "הרצליה", "place", None),
    (r"אילת", "אילת", "place", None),
    (r"ירוחם", "ירוחם", "place", None),
    (r"רמאללה", "רמאללה", "place", None),
    (r"וושינגטון", "וושינגטון די.סי.", "place", None),
    (r"מיאמי", "מיאמי", "place", None),
    (r"איסטנבול", "איסטנבול", "place", "tr"),
    # Countries (with PD flag fallback)
    (r"איראן|איראני", "איראן", "country", "ir"),
    (r"ארצות הברית|ארה\"?ב|אמריק", "ארצות הברית", "country", "us"),
    (r"רוסיה|רוסי", "רוסיה", "country", "ru"),
    (r"אוקראינה", "אוקראינה", "country", "ua"),
    (r"סין|סיני", "סין", "country", "cn"),
    (r"לבנון", "לבנון", "country", "lb"),
    (r"סוריה", "סוריה", "country", "sy"),
    (r"תימן", "תימן", "country", "ye"),
    (r"סעודיה|ערב הסעודית", "ערב הסעודית", "country", "sa"),
    (r"קטאר|קטר", "קטר", "country", "qa"),
    (r"מצרים", "מצרים", "country", "eg"),
    (r"נפאל", "נפאל", "country", "np"),
    (r"איסלנד", "איסלנד", "country", "is"),
    (r"ונצואלה", "ונצואלה", "country", "ve"),
    (r"בריטניה|אנגליה", "הממלכה המאוחדת", "country", "gb"),
    (r"ספרד", "ספרד", "country", "es"),
    (r"טורקיה|טורקי", "טורקיה", "country", "tr"),
    (r"פקיסטן", "פקיסטן", "country", "pk"),
]
_ENTITIES = [(re.compile(pat), title, kind, cc) for pat, title, kind, cc in _ENTITY_SPECS]


def load_image_cache() -> dict:
    try:
        if IMAGE_CACHE_PATH.exists():
            return json.loads(IMAGE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_image_cache(cache: dict) -> None:
    try:
        now = int(time.time())
        pruned = {
            k: v
            for k, v in cache.items()
            if isinstance(v, dict) and now - int(v.get("_ts") or 0) < IMAGE_CACHE_TTL
        }
        IMAGE_CACHE_PATH.write_text(json.dumps(pruned, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[images] cache save failed: {exc}")


def _http_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[images] fetch failed {url[:80]}: {exc}")
        return None


def _item_text(item: dict) -> str:
    parts = [item.get("title") or "", item.get("dryTitle") or "", item.get("summary") or ""]
    for s in item.get("sources") or []:
        parts.append(s.get("headline") or "")
    return " ".join(parts)


def pick_entity(text: str, title: str = "") -> tuple[str, str, str | None] | None:
    """Return (wiki_title, kind, cc) for the story's main entity.

    Prefer the entity that appears earliest in the headline (usually the subject),
    so "בן גביר נגד נתניהו" resolves to Ben Gvir, not the ever-present Netanyahu.
    """
    if title:
        best: tuple[str, str, str | None] | None = None
        best_pos = 10**9
        for rx, wiki, kind, cc in _ENTITIES:
            m = rx.search(title)
            if m and m.start() < best_pos:
                best_pos, best = m.start(), (wiki, kind, cc)
        if best:
            return best
    blob = text or ""
    for rx, wiki, kind, cc in _ENTITIES:
        if rx.search(blob):
            return wiki, kind, cc
    return None


def _commons_filename(url: str) -> str | None:
    """Extract the Commons File name from an upload.wikimedia.org URL."""
    if "/wikipedia/commons/" not in url:
        return None  # local (often non-free) upload — skip
    url = url.split("?", 1)[0].split("#", 1)[0]  # drop utm/query + fragment
    tail = url.split("/wikipedia/commons/", 1)[1]
    parts = tail.split("/")
    if parts and parts[0] == "thumb":
        # thumb/a/ab/Name.jpg/900px-Name.jpg  → Name.jpg
        name = parts[3] if len(parts) >= 4 else None
    else:
        name = parts[-1] if parts else None
    return urllib.parse.unquote(name) if name else None


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _commons_meta(filename: str) -> dict | None:
    """License + attribution + a sized thumb for a Commons file, if free to use."""
    api = (
        "https://commons.wikimedia.org/w/api.php?action=query&format=json&formatversion=2"
        "&prop=imageinfo&iiprop=extmetadata|url&iiurlwidth="
        + str(THUMB_WIDTH)
        + "&titles=File:"
        + urllib.parse.quote(filename)
    )
    data = _http_json(api)
    try:
        info = data["query"]["pages"][0]["imageinfo"][0]
    except (KeyError, IndexError, TypeError):
        return None
    meta = info.get("extmetadata") or {}
    license_short = _strip_html((meta.get("LicenseShortName") or {}).get("value") or "")
    license_code = ((meta.get("License") or {}).get("value") or "").strip()
    if not (_LICENSE_OK.search(license_short) or _LICENSE_OK.search(license_code)):
        return None  # non-free / unknown license — do not use
    artist = _strip_html((meta.get("Artist") or {}).get("value") or "") or "ויקישיתוף"
    if len(artist) > 60:
        artist = artist[:57].rstrip() + "…"
    url = info.get("thumburl") or info.get("url")
    if not url:
        return None
    credit = f"{artist} · {license_short or license_code} · Wikimedia Commons"
    return {
        "url": url,
        "credit": credit,
        "link": "https://commons.wikimedia.org/wiki/File:" + urllib.parse.quote(filename),
    }


def _wikimedia_image(wiki_title: str) -> dict | None:
    summary = _http_json(
        "https://he.wikipedia.org/api/rest_v1/page/summary/"
        + urllib.parse.quote(wiki_title.replace(" ", "_"))
    )
    if not summary:
        return None
    src = (summary.get("originalimage") or {}).get("source") or (
        summary.get("thumbnail") or {}
    ).get("source")
    if not src:
        return None
    filename = _commons_filename(src)
    if not filename:
        return None
    time.sleep(0.2)  # be polite to the Commons API
    return _commons_meta(filename)


def _flag_image(cc: str) -> dict:
    return {
        "url": f"https://flagcdn.com/w640/{cc}.png",
        "credit": "דגל לאומי · נחלת הכלל",
        "link": f"https://flagcdn.com/{cc}.svg",
    }


def image_for_item(item: dict, cache: dict) -> dict | None:
    """Choose a copyright-safe image for a story, or None to use category art."""
    ent = pick_entity(_item_text(item), title=item.get("title") or item.get("dryTitle") or "")
    if not ent:
        return None
    wiki_title, _kind, cc = ent
    key = "wiki:" + wiki_title
    now = int(time.time())
    hit = cache.get(key)
    if isinstance(hit, dict) and now - int(hit.get("_ts") or 0) < IMAGE_CACHE_TTL:
        result = hit.get("result")
    else:
        result = _wikimedia_image(wiki_title)
        cache[key] = {"result": result, "_ts": now}
    if result:
        return result
    if cc:
        return _flag_image(cc)
    return None
