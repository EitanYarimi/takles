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
DISTILL_VERSION = "v8-facts-analysis"
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
KICKER_RE = re.compile(
    r"^(?:מאחורי הקלעים|בלחץ אמריקני|בלעדי|דחוף|עכשיו|צפו|פרשנות|ניתוח|"
    r"לאחר נסיגת צה[\"׳']?ל|הערב|הבוקר|הלילה|דיווח)"
    r"\s*[:\-–—]\s*",
    re.I,
)

PROMPT = """אתה עורך זיקוק עובדתי לפורטל "תכל׳ס" (ישראל).
קלט: כותרת מקבץ + כותרות ממקורות שונים על אותו אירוע.
החזר JSON בלבד (בלי markdown) עם השדות:
- title: כותרת יבשה בעברית, בלי דרמה/קליקבייט
- bullet_facts: מערך 4–6 נתונים/עובדות שניתן לגזור מהקלט בלבד (מי/מה/איפה/מתי/מספרים/סטטוס דיווח). כל פריט = משפט עובדתי קצר. אם מספר לא מופיע בקלט — אל תמציא.
- summary: ניתוח יבש של 3–5 משפטים: מה משותף בין המקורות, מה מדווח רק בחלק מהם, ומה עדיין לא מאושר. זה לא דעה ולא המלצה.
- why_matters: משפט אחד על השלכה עובדתית/ציבורית שנגזרת מהנתונים (לא פרשנות פוליטית, לא ניחוש)
- background: 2–3 משפטים הקשר עובדתי מקדים (רק אם נתמך מהקלט או ידע כללי יציב כמו שמות מוסדות/מקומות)
- outlook: 2 משפטים — שאלות פתוחות / מה עוד חסר לאימות (לא תחזית)
- status: אחד מ־confirmed | reported | denied | review

כללים מחמירים:
- עברית יבשה, בלי פעלים דרמטיים, בלי לינקים, בלי ספינים.
- אסור להמציא עובדות, מספרים, ציטוטים או כוונות שלא בקלט.
- אם המקורות חלוקים — ציין את המחלוקת במפורש ב־summary; אל תבחר צד.
- אל תכתוב דעה, שיפוט מוסרי, או "צריך/ראוי".
- אל תכתוב מטא על האתר ("תכלס", "לוח", "ספין").
- confirmed רק אם לפחות שני מקורות מציגים אותה עובדה בלי לשון ספק; אחרת reported/review.
"""


def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def dry_title(title: str) -> str:
    t = title or ""
    t = KICKER_RE.sub("", t)
    for w in DRAMATIC:
        t = t.replace(w, "")
    t = re.sub(r"\s{2,}", " ", t).strip(" -:·|\"״„“”?")
    return t or (title or "").strip()


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


def _extract_fact_points(title: str, headlines: list[str]) -> list[str]:
    """Pull concrete angles from source headlines instead of pasting spin."""
    blob = " ".join([title, *headlines])
    points: list[str] = []

    def add(s: str) -> None:
        s = dry_title(s).strip(" .")
        if not s:
            return
        if s and s not in points:
            points.append(s)

    # מספרים / כמויות שמופיעים בקלט בלבד
    for m in re.finditer(
        r"(\d+(?:[.,]\d+)?)\s*(%|אחוז|הרוג|הרוגים|פצוע|פצועים|רקטות?|יירוטים?|שעות?|ימים?|ק[\"׳']?מ)",
        blob,
    ):
        add(f"נתון מהדיווחים: {m.group(0)}")

    if re.search(r"נסיגה|פריס|מוקדים|חלק מהרצועה", blob):
        add("במערכת הביטחון / צה״ל נבחנת אפשרות לנסיגה מחלק מהמוקדים ברצועת עזה")
    if re.search(r"אמריקנ|ארה[\"׳']?ב|וושינגטון|טראמפ", blob) and re.search(
        r"נסיגה|לחץ|בוחן", blob
    ):
        add("הדיון מתואר כמתנהל גם תחת לחץ אמריקני")
    if re.search(r"רב-?לאומי|כוח בינלאומי|כוח רב", blob):
        add("נבחנת החלפת נוכחות צה״ל בחלק מהשטח בכוח רב־לאומי מצומצם")
    if re.search(r"פירוז", blob):
        add("מדווחים שתוכנית לפירוז עזה כבר מקודמת גם בלי הסכם סופי")
    if re.search(r"מיליצי|מילצי", blob):
        add("יש דיון נפרד איך ישראל תתייחס למיליציות מקומיות אחרי שינוי פריסה")
    if re.search(r"חטופ|שבוי", blob):
        add("המהלך נשקל על רקע סוגיית החטופים והלחימה ברצועה")
    if re.search(r"הורמוז", blob):
        add("הדיווח נוגע למעבר השיט במצר הורמוז")
    if re.search(r"איראן", blob) and re.search(r"הסכם|עומאן|פתיח", blob):
        add("מדווח על דיונים/הסכמות הקשורים לאיראן ולפתיחת נתיב שיט")

    # זוויות ייחודיות מכותרות המקור (בלי כפילות)
    for line in _unique_lines([title, *headlines], limit=6):
        add(line)

    return points[:6]


def _build_summary(title: str, headlines: list[str], names: list[str]) -> str:
    """Dry cross-source analysis — no opinion, no invented facts."""
    points = _extract_fact_points(title, headlines)
    n = len(names)
    unique_heads = _unique_lines(headlines or [title], limit=4)

    parts: list[str] = []
    if n >= 2:
        parts.append(
            f"הצלבה בין {n} מקורות ({', '.join(names[:4])}{'…' if n > 4 else ''}) על אותו מקבץ."
        )
    else:
        parts.append("מבוסס על מקור יחיד בפיד — עדיין בלי הצלבה מספקת.")

    if len(unique_heads) >= 2:
        parts.append(
            "הקו המשותף בכותרות: "
            + "; ".join(h.rstrip(".") for h in unique_heads[:2])
            + "."
        )
        if len(unique_heads) >= 3:
            parts.append(
                "זווית נוספת שמופיעה רק בחלק מהמקורות: "
                + unique_heads[2].rstrip(".")
                + "."
            )
    elif points:
        parts.append(f"מהדיווח עולה: {points[0].rstrip('.')}.")

    soft = re.search(r"עשוי|אולי|חשד|דיווח|נמסר|לפי גורמ|לטענת|לא אושר|טרם אושר", title)
    if soft or n < 2:
        parts.append("חלק מהפרטים עדיין ברמת דיווח ולא אושרו סופית במקורות שנבדקו.")
    else:
        parts.append("אין בקלט הבהרה מלאה של לוח זמנים, היקף או גורם מאשר.")

    return " ".join(parts)


def _substantive_fallback(title: str, headlines: list[str], names: list[str]) -> tuple[str, str, str]:
    """Build useful background/outlook/why — never product meta-text."""
    points = _extract_fact_points(title, headlines)
    who = f" לפי {', '.join(names[:3])}" if names else ""
    if len(points) >= 2:
        background = (
            f"הדיווח{who} מתרכז בכמה קווים עובדתיים: {'; '.join(points[:3])}."
        )
        outlook = (
            f"עדיין פתוח: האם «{points[0]}» יאושר רשמית, ובאיזה היקף ולוח זמנים. "
            "חסרים פרטים על הגורם המאשר ועל ההשלכות המעשיות."
        )
        why_matters = (
            f"הדיווח נוגע ל־{points[0]} — שינוי במצב הזה ישפיע ישירות על המציאות בשטח או במדיניות."
        )
    else:
        background = (
            f"הדיווח{who} מתאר: {dry_title(title).rstrip('.')}. "
            "עדיין חסר פירוט מצולב בפיד."
        )
        outlook = "חסר אישור רשמי או פירוט על לוח זמנים, היקף והשלכות מעשיות."
        why_matters = (
            "הדיווח מתאר מהלך שעדיין לא סגור — אישור או דחייה ישנו את המצב בפועל."
        )
    return background, outlook, why_matters


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

    bullets = _extract_fact_points(title, headlines)
    while len(bullets) < 3:
        extras = _unique_lines([title, *headlines], limit=5)
        for e in extras:
            if e not in bullets:
                bullets.append(e)
            if len(bullets) >= 3:
                break
        break
    while len(bullets) < 3:
        bullets.append("חלק מהפרטים עדיין לא הובהרו במקורות שנבדקו.")

    summary = _build_summary(title, headlines, names)

    blob = " ".join([title, *headlines])
    if re.search(r"הורמוז|איראן", blob):
        why_matters = "זה משפיע על שיט ואנרגיה גלובליים — ולכן גם על מחירים ויציבות שנוגעים לישראל ולשווקים."
        background = (
            "מצר הורמוז הוא נתיב שיט מרכזי לנפט ולסחר במפרץ. "
            "מתיחות בין איראן לארה״ב ולמדינות האזור סביב פתיחה/סגירה של המעבר משפיעה על מחירים, ביטוח אוניות ויציבות אזורית."
        )
        outlook = (
            "מה שחשוב לעקוב אחריו: האם יש הסכמה מעשית על פתיחת המעבר, ומה אומרות איראן, ארה״ב ומדינות המפרץ בפועל. "
            "שינוי חד בהצהרות או בתנועת אוניות עשוי לעדכן את התמונה במהירות."
        )
    elif re.search(
        r"חיזבאללה|לבנון|רחפן|יירוט|פיקוד העורף|חטופ|עזה|חמאס|רצועת|נסיגה|צה[\"׳']?ל|מערכת הביטחון|כוחות|לחימ",
        blob,
    ):
        if re.search(r"עזה|חמאס|רצועת|נסיגה", blob):
            points = _extract_fact_points(title, headlines)
            why_matters = (
                "כי נסיגה ממוקדים בעזה — במיוחד תחת לחץ אמריקני ובדיון על כוח רב־לאומי — משנה שליטה בשטח, לחימה ומיקוח מדיני."
                if re.search(r"רב-?לאומי|אמריקנ", blob)
                else "כי דיון על נסיגה או שינוי פריסה בעזה משפיע ישירות על הלחימה, על החטופים ועל הלחץ המדיני סביב הרצועה."
            )
            background = (
                "ממקורות שונים עולה אותו דיון בכמה זוויות: "
                + "; ".join(points[:4])
                + ". "
                "עדיין מדובר בבחינה/דיווח ולא בהכרח בהחלטה סופית מאושרת."
            )
            outlook = (
                "השלב הבא: האם יש החלטה מאושרת, מאילו מוקדים, האם נכנס כוח חלופי, ובאיזה לוח זמנים — או שהבדיקה נגנזת. "
                "תגובות פוליטיות ודיווחי שטח יבהירו אם מדובר במהלך ממשי."
            )
        else:
            why_matters = "כי זה נוגע ישירות לביטחון תושבים ולמצב בגבול — לא לאירוע רחוק בלבד."
            background = (
                "זירת הצפון מול לבנון נשארת רגישה: דיווחים על רחפנים, יירוטים או חילופי אש משפיעים על תושבים וכוחות. "
                "גם כשהאירוע נקודתי, הוא מגיע על רקע מתיחות מתמשכת."
            )
            outlook = (
                "לעקוב אחרי אישור רשמי של צה״ל/פיקוד העורף, היקף הנזק או היירוט, והאם מגיע אירוע המשך. "
                "שינוי בקצב התקריות הוא הסימן הכי שימושי."
            )
    elif re.search(
        r"תובע הכללי|בא.?כוח הכללי|עוה\"?ד|עורך דין|סנאט|הבית הלבן|טראמפ|ביידן|בלנש|בלאנש|Blanche|ארה[\"׳']?ב|וושינגטון|פנטגון|נאט.?ו",
        blob,
    ):
        why_matters = "מינוי בכיר בוושינגטון קובע מדיניות אכיפה ותיקים רגישים — ולכן משפיע גם על זירה בינלאומית שישראל עוקבת אחריה."
        background = (
            "במערכת האמריקאית, מינוי לתפקיד בכיר כמו תובע כללי עובר בדרך כלל דרך נשיא וסנאט, "
            "ומשפיע על מדיניות אכיפה, תיקים רגישים ויחסי חוץ. "
            "כשמונה מישהו שהיה בעבר עורך דין של הנשיא, עולות גם שאלות על עצמאות מוסדית וניגודי עניינים — גם אם המינוי עצמו חוקי."
        )
        outlook = (
            "השלב הבא: האם הסנאט מאשר סופית, מתי נכנסים לתפקיד, ואילו תיקים/קווי מדיניות מסומנים ראשונים. "
            "תגובות במפלגות ובמערכת המשפט האמריקאית יבהירו כמה שנוי במחלוקת המינוי בפועל."
        )
    elif re.search(r"כנסת|ממשלת ישראל|קואליצ|פריימר|רע.?ם|מפלג|נתניהו|לפיד|גנץ", blob):
        why_matters = "זה על סדר היום כי מהלכים פוליטיים כאן משנים יציבות שלטון והחלטות שנוגעות לכולם."
        background = (
            "בפוליטיקה הישראלית, מהלכי מפלגות, פריימריז והחלטות בכנסת משפיעים על יציבות הקואליציה ועל סדר היום. "
            "כותרת כזו בדרך כלל מסמנת מאבק כוח, מועמדות או הסדר פוליטי."
        )
        outlook = (
            "השלב הבא הוא בדרך כלל הודעות רשמיות, הצבעות או סיכום מועמדים. "
            "כדאי לבדוק אם יש הסכמה בין מקורות על העובדות לפני מסקנה על התוצאה הפוליטית."
        )
    elif re.search(r"תייר|נופש|מלונ|חופשה|משרד התיירות", blob):
        why_matters = "זה רלוונטי לכיס ולתוכניות חופשה — מחירים, זמינות ומדיניות שמשפיעים על מטיילים עכשיו."
        background = (
            "ענף התיירות והנופש בישראל רגיש לביטחון, מחירים ועונות חגים. "
            "דיווחים על מלונות, מבצעים או מדיניות משפיעים ישירות על מטיילים ועל עסקים מקומיים."
        )
        outlook = (
            "לעקוב אחרי שינויי מחיר, זמינות לחגים, והודעות של משרד התיירות או הרשתות. "
            "שינוי במדיניות ביטולים או הטבות הוא עדכון פרקטי."
        )
    elif re.search(r"ספורט|כדורגל|מכבי|הפועל|נבחרת|פרמייר|העברה|שחקנ", blob):
        why_matters = "זו כותרת לספורט כי יש כאן שינוי בסגל או בתוצאה שעשוי להשפיע על המשך העונה."
        background = (
            "בספורט יש פער תכוף בין שמועה לאישור רשמי של מועדון או ליגה. "
            "הערך לקורא הוא להפריד בין מה שסוכם לבין מה שעדיין בדיווח בלבד."
        )
        outlook = (
            "לחכות לאישור המועדון/הליגה או לתוצאה הרשמית, ואז לראות השפעה על הסגל ועל לוח המשחקים."
        )
    elif re.search(r"בורסה|ריבית|מני|דולר|ברקשייר|השקע|בנק ישראל", blob):
        why_matters = "זה על הלוח כי מהלך כלכלי כזה יכול להזיז מחירים, חיסכון או תחושת שוק בטווח קצר."
        background = (
            "דיווחים על מניות, ריבית או מהלכי השקעה משפיעים על משקיעים ועל תחושת השוק. "
            "חשוב להבחין בין עובדה שפורסמה לבין פרשנות או המלצה."
        )
        outlook = (
            "לעקוב אחרי תגובת השוק והודעות רשמיות ביום־יומיים הקרובים. "
            "נתון מאושר או דוח מעודכן משנה את התמונה יותר מכותרת פרשנית."
        )
    else:
        background, outlook, why_matters = _substantive_fallback(title, headlines, names)

    insight = ""

    return {
        "title": title,
        "summary": summary,
        "why_matters": why_matters,
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
    why_matters = str(data.get("why_matters") or data.get("whyItMatters") or "").strip()
    title = dry_title(str(data.get("title") or item.get("title") or ""))
    if not background or not outlook:
        return None
    if not summary:
        summary = background
    if not why_matters:
        why_matters = (
            "כי מדובר במהלך שעדיין פתוח — אישור או דחייה ישנו את המציאות בשטח או במדיניות."
        )

    return {
        "title": title,
        "summary": summary,
        "why_matters": why_matters,
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
        and hit.get("why_matters")
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
        enriched["why_matters"] = d.get("why_matters")
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
