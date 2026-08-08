#!/usr/bin/env python3
"""Pre-distill news items: facts, background, outlook (Gemini + heuristic fallback)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "distill_cache.json"
SSL_CTX = ssl._create_unverified_context()
UA = "Mozilla/5.0 ClearNewsPOC/0.5"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
DRAMATIC = (
    "שומט את הקרקע",
    "על חודו של קול",
    "הלם ענק",
    "משתוללת",
    "משתולל",
    "מתפוצצת",
    "מתפוצץ",
    "דרמה",
    "הלם",
    "בלעדי",
    "דחוף",
    "הזוי",
    "מטורף",
    "קריסה",
    "נחשף",
)

PROMPT = """אתה עורך חדשות יבש לפורטל "תכלס" (ישראל).
קלט: כותרת מקבץ + כותרות מקורות.
החזר JSON בלבד (בלי markdown) עם השדות:
- title: כותרת יבשה בעברית, בלי דרמה/קליקבייט
- bullet_facts: מערך 3–5 עובדות קצרות (מי/מה/איפה/מתי/מספרים אם יש)
- background: משפט–שניים של ידע מקדים (למה זה חשוב עכשיו)
- outlook: משפט–שניים זהיר על מה לעקוב / השלכות אפשריות (בלי סנסציה)
- insight: תובנה קצרה אחת על מה שמוסכם בין המקורות
- status: אחד מ־confirmed | reported | denied | review

כללים: עברית, יבש, בלי פעלים דרמטיים, בלי לינקים, אל תמציא מספרים שלא מופיעים בקלט.
"""


def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def dry_title(title: str) -> str:
    t = title or ""
    for w in DRAMATIC:
        t = t.replace(w, "")
    t = re.sub(r"\s{2,}", " ", t).strip(" -:·|")
    return t or title


def item_fingerprint(item: dict) -> str:
    parts = [item.get("title") or ""]
    for s in item.get("sources") or []:
        parts.append(s.get("name") or "")
        parts.append(s.get("headline") or "")
    blob = "|".join(parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_cache(cache: dict) -> None:
    try:
        # keep cache bounded
        if len(cache) > 400:
            keys = sorted(cache.keys(), key=lambda k: cache[k].get("_ts", 0), reverse=True)
            cache = {k: cache[k] for k in keys[:300]}
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[distill] cache save failed: {exc}")


def heuristic_distill(item: dict) -> dict:
    title = dry_title(item.get("title") or "")
    sources = item.get("sources") or []
    headlines = [s.get("headline") or "" for s in sources]
    names = []
    for s in sources:
        n = (s.get("name") or "").strip()
        if n and n not in names:
            names.append(n)

    quoted = bool(re.search(r"\"|״|„|“|”|:", title))
    multi = len(names) >= 2
    status = "reported"
    if multi and not quoted and not re.search(r"עשוי|אולי|חשד|דיווח|נמסר", title):
        status = "confirmed"
    if re.search(r"הכחיש|הכחשה|דחה את", title):
        status = "denied"
    if re.search(r"חשד|אולי|עשוי|נבדק", title):
        status = "review"

    bullets: list[str] = []
    seen: set[str] = set()
    for h in [title, *headlines]:
        dry = dry_title(h)
        key = dry[:40]
        if not dry or key in seen:
            continue
        seen.add(key)
        bullets.append(dry)
        if len(bullets) >= 4:
            break
    while len(bullets) < 3:
        bullets.append(
            f"מקור נוסף: {names[len(bullets)]}"
            if len(bullets) < len(names)
            else "פרטים נוספים טרם הובהרו במקורות שנבדקו"
        )

    insight = (
        f"אותו אירוע מדווח ב-{len(names)} מקורות — מוצג ככרטיס אחד."
        if multi
        else "מבוסס על מקור יחיד בפיד — סטטוס דיווח עד להצלבה."
    )

    blob = " ".join([title, *headlines])
    if re.search(r"הורמוז|איראן", blob):
        background = "סוגיית מצר הורמוז ומתיחות אזורית מול איראן ממשיכות להשפיע על שיט ואנרגיה."
        outlook = "לעקוב אחרי הצהרות איראן/ארה״ב ופתיחה או סגירה בפועל של המעבר."
    elif re.search(r"חיזבאללה|לבנון|רחפן|יירוט", blob):
        background = "זירת הצפון / לבנון פעילה; דיווחים על חילופי אש או יירוטים חוזרים."
        outlook = "לעקוב אחרי אישור צה״ל, היקף האירוע והסלמה נוספת בגבול."
    elif re.search(r"כנסת|ממשלה|פריימר|רע.?ם|בחיר", blob):
        background = "מהלך פוליטי ישראלי פתוח — הכרעות בכנסת/מפלגות משפיעות על יציבות הקואליציה."
        outlook = "לעקוב אחרי הצבעות, הודעות מפלגות וסיכומי פריימריז."
    elif re.search(r"תייר|נופש|מלונ|חופשה", blob):
        background = "ענף התיירות והנופש בישראל רגיש לביטחון, מחירים וחגים."
        outlook = "לעקוב אחרי מבצעי מלונות, מדיניות משרד התיירות וביקוש לחגים."
    elif re.search(r"ספורט|כדורגל|מכבי|הפועל|נבחרת", blob):
        background = "עדכון ספורט שוטף — העברות, תוצאות או סגל."
        outlook = "לעקוב אחרי אישור העסקה/תוצאה רשמית ולוח המשחקים."
    elif re.search(r"בורסה|ריבית|מני|דולר|ברקשייר", blob):
        background = "שווקים ותנודות הון — דיווח כלכלי עם השפעה על משקיעים."
        outlook = "לעקוב אחרי תגובת שוק והודעות רשמיות נוספות."
    else:
        background = "דיווח שוטף מפיד חדשות ישראל; הרקע המלא תלוי בהתפתחויות נוספות."
        outlook = "לעקוב אחרי הצלבת מקורות ועדכונים רשמיים ביממה הקרובה."

    return {
        "title": title,
        "bullet_facts": bullets[:5],
        "background": background,
        "outlook": outlook,
        "insight": insight,
        "historical_context": background,
        "status": status,
        "mode": "heuristic",
    }


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def gemini_distill(item: dict) -> dict | None:
    key = _api_key()
    if not key:
        return None
    sources = item.get("sources") or []
    lines = [f"כותרת מקבץ: {item.get('title') or ''}"]
    for s in sources[:6]:
        lines.append(f"- {s.get('name') or 'מקור'}: {s.get('headline') or ''}")
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": PROMPT + "\n\n" + "\n".join(lines)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45, context=SSL_CTX) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[distill] gemini failed: {exc}")
        return None

    try:
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    data = _extract_json(text)
    if not data:
        return None

    bullets = data.get("bullet_facts") or data.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    bullets = [str(b).strip() for b in bullets if str(b).strip()][:5]
    if len(bullets) < 3:
        return None

    status = data.get("status") or "reported"
    if status not in {"confirmed", "reported", "denied", "review"}:
        status = "reported"
    background = str(data.get("background") or "").strip()
    outlook = str(data.get("outlook") or "").strip()
    insight = str(data.get("insight") or "").strip()
    title = dry_title(str(data.get("title") or item.get("title") or ""))
    if not background or not outlook:
        return None

    return {
        "title": title,
        "bullet_facts": bullets,
        "background": background,
        "outlook": outlook,
        "insight": insight or background,
        "historical_context": background,
        "status": status,
        "mode": "gemini",
    }


def distill_item(item: dict, cache: dict | None = None) -> dict:
    cache = cache if cache is not None else load_cache()
    fp = item_fingerprint(item)
    hit = cache.get(fp)
    if isinstance(hit, dict) and hit.get("bullet_facts") and hit.get("background") and hit.get("outlook"):
        out = {k: v for k, v in hit.items() if not k.startswith("_")}
        return out

    distilled = gemini_distill(item) or heuristic_distill(item)
    cache[fp] = {**distilled, "_ts": int(time.time())}
    save_cache(cache)
    return distilled


def enrich_items(items: list[dict]) -> list[dict]:
    cache = load_cache()
    out = []
    for item in items:
        d = distill_item(item, cache=cache)
        enriched = dict(item)
        enriched["distill"] = d
        # flatten common fields for simpler clients
        enriched["dryTitle"] = d.get("title")
        enriched["bullet_facts"] = d.get("bullet_facts")
        enriched["background"] = d.get("background")
        enriched["outlook"] = d.get("outlook")
        enriched["insight"] = d.get("insight")
        enriched["historical_context"] = d.get("historical_context")
        enriched["status"] = d.get("status")
        enriched["distillMode"] = d.get("mode")
        out.append(enriched)
    save_cache(cache)
    return out
