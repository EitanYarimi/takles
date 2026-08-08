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
DISTILL_VERSION = "v2-summary"
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
- summary: פסקה של 2–4 משפטים שמסבירה לקורא מה קרה בפועל ולמה זה משנה — לא חזרה על הכותרת במילה אחת, אלא הסבר קריא
- bullet_facts: מערך 3–5 עובדות ברורות (מי/מה/איפה/מתי/מספרים אם יש) — כל פריט משפט מלא, לא שבריר כותרת
- background: 2–3 משפטים של ידע מקדים (רקע שהקורא צריך כדי להבין את הכותרת)
- outlook: 2 משפטים זהירים על מה לעקוב / השלכות אפשריות (בלי סנסציה)
- insight: תובנה קצרה על מה שמוסכם או חלוק בין המקורות
- status: אחד מ־confirmed | reported | denied | review

כללים: עברית, יבש, בלי פעלים דרמטיים, בלי לינקים, אל תמציא מספרים שלא מופיעים בקלט.
אל תכתוב משפטים של 4–6 מילים בלבד — תן הסבר מספיק כדי שאפשר להבין בלי לקרוא את המקור.
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
    parts = [DISTILL_VERSION, item.get("title") or ""]
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
        if len(cache) > 400:
            keys = sorted(cache.keys(), key=lambda k: cache[k].get("_ts", 0), reverse=True)
            cache = {k: cache[k] for k in keys[:300]}
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[distill] cache save failed: {exc}")


def _unique_lines(lines: list[str], limit: int = 5) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        dry = dry_title(raw)
        if not dry:
            continue
        key = re.sub(r"\s+", " ", dry).strip().lower()[:56]
        if key in seen:
            continue
        seen.add(key)
        out.append(dry)
        if len(out) >= limit:
            break
    return out


def _build_summary(title: str, headlines: list[str], names: list[str]) -> str:
    extras = _unique_lines([h for h in headlines if dry_title(h) != title], limit=3)
    parts = [
        f"לפי הדיווחים, {title.rstrip('.')}."
    ]
    if extras:
        parts.append(
            "פרטים נוספים שעולים מהמקורות: "
            + "; ".join(extras[:2])
            + "."
        )
    if len(names) >= 2:
        parts.append(
            f"הנושא מדווח במקביל ב־{len(names)} מקורות ({', '.join(names[:3])}"
            + ("…" if len(names) > 3 else "")
            + "), ולכן מוצג ככרטיס אחד עם עובדות מצולבות."
        )
    else:
        parts.append(
            "כרגע זה מבוסס בעיקר על מקור אחד בפיד — כדאי לקרוא את הרקע ואת המבט קדימה לפני מסקנה חזקה."
        )
    return " ".join(parts)


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

    bullets = _unique_lines([title, *headlines], limit=4)
    while len(bullets) < 3:
        bullets.append(
            f"דיווח נוסף מ־{names[len(bullets)]}: יש לעקוב אחרי הצלבה."
            if len(bullets) < len(names)
            else "חלק מהפרטים עדיין לא הובהרו במקורות שנבדקו בפיד."
        )

    summary = _build_summary(title, headlines, names)

    blob = " ".join([title, *headlines])
    if re.search(r"הורמוז|איראן", blob):
        background = (
            "מצר הורמוז הוא נתיב שיט מרכזי לנפט ולסחר במפרץ. "
            "מתיחות בין איראן לארה״ב ולמדינות האזור סביב פתיחה/סגירה של המעבר משפיעה על מחירים, ביטוח אוניות ויציבות אזורית — ולכן דיווחים כאלה נכנסים ללוח גם כשהאירוע רחוק גיאוגרפית מישראל."
        )
        outlook = (
            "מה שחשוב לעקוב אחריו: האם יש הסכמה מעשית על פתיחת המעבר, ומה אומרות איראן, ארה״ב ומדינות המפרץ בפועל. "
            "שינוי חד בהצהרות או בתנועת אוניות עשוי לעדכן את התמונה במהירות."
        )
    elif re.search(r"חיזבאללה|לבנון|רחפן|יירוט", blob):
        background = (
            "זירת הצפון מול לבנון נשארת רגישה: דיווחים על רחפנים, יירוטים או חילופי אש משפיעים על תושבים, כוחות וסדרי עדיפויות ביטחוניים. "
            "גם כשהאירוע נקודתי, הוא מגיע על רקע מתיחות מתמשכת ולא כאירוע מבודד."
        )
        outlook = (
            "לעקוב אחרי אישור רשמי של צה״ל/פיקוד העורף, היקף הנזק או היירוט, והאם מגיע אירוע המשך באותה זירה. "
            "שינוי בקצב התקריות הוא הסימן הכי שימושי לקורא."
        )
    elif re.search(r"כנסת|ממשלה|פריימר|רע.?ם|בחיר", blob):
        background = (
            "בפוליטיקה הישראלית, מהלכי מפלגות, פריימריז והחלטות בכנסת משפיעים על יציבות הקואליציה ועל סדר היום הציבורי. "
            "כותרת כזו בדרך כלל מסמנת מאבק כוח, מועמדות או הסדר פוליטי — לא רק ״רעש״ חד־יומי."
        )
        outlook = (
            "השלב הבא הוא בדרך כלל הודעות רשמיות, הצבעות או סיכום מועמדים. "
            "כדאי לבדוק אם יש הסכמה בין מקורות על העובדות הבסיסיות לפני שמסיקים על התוצאה הפוליטית."
        )
    elif re.search(r"תייר|נופש|מלונ|חופשה", blob):
        background = (
            "ענף התיירות והנופש בישראל רגיש לביטחון, מחירים ועונות חגים. "
            "דיווחים על מלונות, מבצעים או מדיניות משרד התיירות משפיעים ישירות על מטיילים ועל עסקים מקומיים."
        )
        outlook = (
            "לעקוב אחרי שינויי מחיר, זמינות חדרים לקראת חגים, והודעות רשמיות של משרד התיירות או הרשתות. "
            "שינוי מדיניות ביטולים/הטבות הוא עדכון פרקטי לקוראים."
        )
    elif re.search(r"ספורט|כדורגל|מכבי|הפועל|נבחרת|פרמייר", blob):
        background = (
            "בספורט, כותרות על העברות, תוצאות או סגל משתנות במהירות ויש פער בין שמועה לאישור רשמי. "
            "הערך לקורא הוא להפריד בין מה שסוכם לבין מה שעדיין בדיווח בלבד."
        )
        outlook = (
            "לחכות לאישור המועדון/הליגה או לתוצאה הרשמית, ואז לראות איך זה משפיע על הסגל ועל לוח המשחקים. "
            "עדכון חוזי או הודעת מועדון בדרך כלל סוגר את אי־הוודאות."
        )
    elif re.search(r"בורסה|ריבית|מני|דולר|ברקשייר|השקע", blob):
        background = (
            "דיווחים כלכליים על מניות, ריבית או מהלכי השקעה משפיעים על משקיעים ועל תחושת השוק. "
            "חשוב להבחין בין עובדה שפורסמה (עסקה/נתון) לבין פרשנות או המלצה."
        )
        outlook = (
            "לעקוב אחרי תגובת השוק והודעות רשמיות נוספות ביום־יומיים הקרובים. "
            "נתון מאושר או דוח מעודכן ישנה את התמונה יותר מכותרת פרשנית."
        )
    else:
        background = (
            f"הכותרת עוסקת ב־«{title}». "
            "כדי להבין אותה חשוב לראות מי מדווח, אילו פרטים חוזרים בין מקורות, ומה עדיין לא מאומת. "
            "הלוח מציג את זה ככרטיס עובדות יבש במקום אוסף כותרות מתחרות."
        )
        outlook = (
            "המשך מעקב אחרי הצלבת מקורות ועדכונים רשמיים ביממה הקרובה. "
            "אם יתווספו מספרים, שמות גורמים או הכחשה — הסטטוס והסיכום יתעדכנו בהתאם."
        )

    insight = (
        f"יש חפיפה בין {len(names)} מקורות על אותו אירוע; הסיכום מנסה להציג את המשותף ולא את הספין."
        if multi
        else "מקור יחיד בפיד: הסיכום זהיר יותר עד שתהיה הצלבה."
    )

    return {
        "title": title,
        "summary": summary,
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
    summary = str(data.get("summary") or "").strip()
    title = dry_title(str(data.get("title") or item.get("title") or ""))
    if not background or not outlook:
        return None
    if not summary:
        # soft fallback from background if model omitted summary
        summary = background

    return {
        "title": title,
        "summary": summary,
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
    if (
        isinstance(hit, dict)
        and hit.get("bullet_facts")
        and hit.get("background")
        and hit.get("outlook")
        and hit.get("summary")
    ):
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
        enriched["dryTitle"] = d.get("title")
        enriched["summary"] = d.get("summary")
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
