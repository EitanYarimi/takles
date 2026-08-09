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
DISTILL_VERSION = "v9-no-opinion-clusters"
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
OPINION_LABEL_RE = re.compile(
    r"(?:\|\s*)?(?:דעה|פרשנות|טור(?:\s+דעה)?)\s*$|"
    r"(?:\|\s*)(?:דעה|פרשנות|טור)\b|"
    r"^(?:דעה|פרשנות|טור)\s*[:\-–—]",
    re.I,
)
OPINION_COLOR_RE = re.compile(
    r"הצצה לתוך|השיגעון|רק מחזק את הזעזוע|לא מחדשות דבר",
    re.I,
)
FACT_SIGNAL_RE = re.compile(
    r"מגיב|הודיע|מסר|נמסר|אישר|הכחיש|דחה|נעצר|נפצע|נהרג|הורשע|"
    r"הצביע|אושר|מינה|פיטר|חתם|שיגר|ירה|יי.?רט|התרע|אזעק|"
    r"העלה|הוריד|קבע|פרסם|דיווח|חק[רה]|הגיש|התפטר|הכריז",
    re.I,
)
QUOTE_REACTION_RE = re.compile(
    r"(מגיב|בתגובה|תוקף|מתוקף).{0,40}[:\"״]|[\"״].{15,}[\"״]",
    re.I,
)


def _strip_quotes_and_tail(text: str) -> str:
    t = text or ""
    t = re.sub(r"[\"״„“”].*?[\"״„“”]", " ", t)
    t = re.split(r"[:\-–—]\s*", t, maxsplit=1)[0]
    return re.sub(r"\s+", " ", t).strip()


def is_opinion_text(text: str) -> bool:
    t = text or ""
    if OPINION_LABEL_RE.search(t):
        return True
    core = _strip_quotes_and_tail(t)
    if FACT_SIGNAL_RE.search(core):
        return False
    if OPINION_COLOR_RE.search(t) or OPINION_COLOR_RE.search(core):
        return True
    # כותרת רטורית/מטאפורית בלי פועל עובדתי
    if len(core) < 48 and not FACT_SIGNAL_RE.search(core) and not re.search(r"\d", core):
        if re.search(r"[?!]|שיגעון|זעזוע|הלם|דרמה", t):
            return True
    return False


def looks_like_fact_line(text: str) -> bool:
    t = dry_title(text or "")
    if not t or len(t) < 12:
        return False
    if is_opinion_text(t):
        return False
    if FACT_SIGNAL_RE.search(t) or re.search(r"\d", t):
        return True
    # משפט עם שני שמות/תפקידים לפחות ופעלת דיווח עדינה
    if re.search(r"של |מול |בין ", t) and re.search(r"על |לגבי |בנוגע", t):
        return True
    return False


def factual_core_title(title: str) -> str:
    """Strip quoted spin after a reaction lead-in."""
    t = dry_title(title or "")
    # יועץ X מגיב ל-Y: "ספין..." → שמור רק את הליבה
    m = re.match(
        r'^(.{8,80}?(?:מגיב|הודיע|מסר|אישר|הכחיש)\s+ל[^:\"״]{2,40})\s*[:\-–—]\s*[\"״].*',
        t,
    )
    if m:
        return m.group(1).strip(" -:·")
    # חתוך ציטוט ארוך אחרי נקודתיים
    if re.search(r'[:\-–—]\s*[\"״]', t):
        t = re.split(r'[:\-–—]\s*[\"״]', t, maxsplit=1)[0].strip(" -:·")
    return t or dry_title(title or "")


def scrub_item_sources(item: dict) -> dict | None:
    """Keep hard-news sources only. Drop opinion-only clusters."""
    sources = list(item.get("sources") or [])
    factual = []
    opinion = []
    for s in sources:
        head = s.get("headline") or ""
        if is_opinion_text(head):
            opinion.append(s)
        else:
            factual.append(s)
    title = item.get("title") or ""
    if not factual:
        # אם הכותרת עצמה עובדתית — שמור מקור יחיד סינתטי
        if looks_like_fact_line(title) and not is_opinion_text(title):
            factual = [
                {
                    "url": item.get("link") or "",
                    "headline": factual_core_title(title),
                    "name": (sources[0].get("name") if sources else None) or "Google News",
                }
            ]
            opinion = sources
        else:
            return None

    # רעש פוליטי: תגובת ציטוט + בעיקר דעות
    core = factual_core_title(title)
    if (
        QUOTE_REACTION_RE.search(title)
        and len(opinion) >= max(1, len(factual))
        and not re.search(r"יי.?רט|אזעק|רקטות?|חטופ|מלחמ|פיגוע", title)
    ):
        return None

    out = dict(item)
    out["title"] = core
    out["sources"] = factual[:6]
    if opinion:
        out["opinion_sources"] = opinion[:6]
    return out


PROMPT = """אתה עורך זיקוק עובדתי לפורטל "תכל׳ס" (ישראל).
קלט: כותרת מקבץ + כותרות ממקורות חדשותיים בלבד (בלי דעות).
החזר JSON בלבד (בלי markdown) עם השדות:
- title: כותרת יבשה בעברית, בלי דרמה/קליקבייט/ציטוטי ספין
- bullet_facts: מערך 3–6 נתונים שניתן לגזור מהקלט בלבד. אסור להעתיק כותרות דעה/פרשנות. אסור ציטוטי התקפה פוליטית כעובדה — רק מי אמר/הגיב למי אם זה הדיווח.
- summary: ניתוח יבש 2–4 משפטים: מה אומת יחסית, מה דיווח יחיד, מה חסר. בלי דעה.
- why_matters: השלכה עובדתית אחת שנגזרת מהנתונים, או מחרוזת ריקה אם אין השלכה ברורה
- background: הקשר עובדתי קצר ורק אם נתמך; אחרת משפט אחד "חסר הקשר מצולב בקלט"
- outlook: מה עוד לא ברור לאימות (לא תחזית)
- status: confirmed | reported | denied | review

כללים:
- אל תמציא. אל תבחר צד. אל תשתמש במילות דרמה.
- אם הקלט הוא בעיקר תגובה פוליטית/ציטוט — כתוב רק את ליבת האירוע (מי הגיב למי) וציין שחסר פירוט עובדתי.
- confirmed רק עם לפחות שני מקורות על אותה עובדה בלי לשון ספק.
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
    """Pull concrete angles from hard-news headlines only."""
    factual_heads = [h for h in headlines if looks_like_fact_line(h)]
    blob = " ".join([title, *factual_heads])
    points: list[str] = []

    def add(s: str) -> None:
        s = dry_title(s).strip(" .")
        if not s or is_opinion_text(s):
            return
        if s and s not in points:
            points.append(s)

    core = factual_core_title(title)
    if looks_like_fact_line(core):
        add(core)

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

    for line in _unique_lines(factual_heads, limit=6):
        if looks_like_fact_line(line):
            add(line)

    return points[:6]


def _build_summary(title: str, headlines: list[str], names: list[str]) -> str:
    """Dry cross-source analysis — no opinion, no invented facts."""
    factual_heads = [h for h in headlines if looks_like_fact_line(h)]
    points = _extract_fact_points(title, factual_heads)
    n = len(names)
    unique_heads = _unique_lines(factual_heads or ([factual_core_title(title)] if title else []), limit=4)

    parts: list[str] = []
    if n >= 2:
        parts.append(
            f"הצלבה בין {n} מקורות חדשותיים ({', '.join(names[:4])}{'…' if n > 4 else ''})."
        )
    else:
        parts.append("מבוסס על מקור חדשותי יחיד — בלי הצלבה מספקת.")

    if unique_heads:
        parts.append("מה שניתן לגזור מהדיווחים: " + "; ".join(h.rstrip(".") for h in unique_heads[:3]) + ".")
    elif points:
        parts.append(f"מהדיווח עולה: {points[0].rstrip('.')}.")
    else:
        parts.append("לא נמצאו עובדות יציבות מעבר לכותרת המקבץ.")

    soft = re.search(r"עשוי|אולי|חשד|דיווח|נמסר|לפי גורמ|לטענת|לא אושר|טרם אושר", title)
    if soft or n < 2 or QUOTE_REACTION_RE.search(title or ""):
        parts.append("חלק מהפרטים ברמת דיווח/תגובה — לא אומתו מעבר לכך במקורות שנבדקו.")
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
    title = factual_core_title(item.get("title") or "")
    sources = item.get("sources") or []
    headlines = [
        s.get("headline") or ""
        for s in sources
        if not is_opinion_text(s.get("headline") or "")
    ]
    if not headlines:
        headlines = [title] if title else []
    names = []
    for s in sources:
        if is_opinion_text(s.get("headline") or ""):
            continue
        n = (s.get("name") or "").strip()
        if n and n not in names:
            names.append(n)

    raw_title = item.get("title") or ""
    quoted = bool(re.search(r"\"|״|„|“|”", raw_title))
    soft_claim = bool(
        re.search(r"עשוי|אולי|חשד|דיווח|נמסר|לפי גורמ|לטענת|לא אושר|טרם אושר", title)
    )
    multi = len(names) >= 2
    status = "reported"
    if multi and not quoted and not soft_claim and not QUOTE_REACTION_RE.search(raw_title):
        status = "confirmed"
    if re.search(r"הכחיש|הכחשה|דחה את", title):
        status = "denied"
    if re.search(r"חשד|אולי|עשוי|נבדק", title) or QUOTE_REACTION_RE.search(raw_title):
        status = "review"

    bullets = [
        b
        for b in _extract_fact_points(title, headlines)
        if looks_like_fact_line(b) or str(b).startswith("נתון")
    ]
    if not bullets and title:
        bullets = [title]
    while len(bullets) < 3:
        bullets.append("חלק מהפרטים העובדתיים עדיין לא הובהרו במקורות החדשותיים שנבדקו.")

    summary = _build_summary(title, headlines, names)
    blob = " ".join([title, *headlines])
    thin_reaction = bool(QUOTE_REACTION_RE.search(raw_title)) and len(headlines) <= 2

    if thin_reaction:
        background = "חסר הקשר מצולב מעבר לדיווח על התגובה עצמה."
        outlook = "חסר פירוט עובדתי: על מה בדיוק הגיבו, ומה אומת מעבר לציטוט."
        why_matters = ""
    elif re.search(r"הורמוז|איראן", blob):
        why_matters = "הדיווח נוגע לנתיב שיט מרכזי — שינוי שם משפיע על אנרגיה ומחירים."
        background = (
            "מצר הורמוז הוא נתיב שיט מרכזי לנפט ולסחר במפרץ. "
            "דיווחים על פתיחה/סגירה או הסכמות סביבו נוגעים לשיט ולשווקים."
        )
        outlook = (
            "עדיין פתוח: האם יש הסכמה מעשית על פתיחת המעבר, ומה נאמר בפועל ע״י הצדדים. "
            "חסרים פרטי לוח זמנים והיקף."
        )
    elif re.search(
        r"חיזבאללה|לבנון|רחפן|יירוט|פיקוד העורף|חטופ|עזה|חמאס|רצועת|נסיגה|צה[\"׳']?ל|מערכת הביטחון|כוחות|לחימ",
        blob,
    ):
        points = _extract_fact_points(title, headlines)
        why_matters = "הדיווח נוגע לביטחון תושבים, לגבול או ללחימה."
        background = (
            "ממקורות חדשותיים: "
            + ("; ".join(points[:4]) if points else title)
            + ". עדיין מדובר בדיווח שדורש אישור/פירוט."
        )
        outlook = "חסרים אישור רשמי, היקף ולוח זמנים."
    elif re.search(
        r"תובע הכללי|סנאט|הבית הלבן|טראמפ|ביידן|ארה[\"׳']?ב|וושינגטון|פנטגון|נאט.?ו",
        blob,
    ):
        why_matters = "מינוי או מהלך בכיר בוושינגטון משפיע על מדיניות אכיפה ותיקים רגישים."
        background = "במערכת האמריקאית חשוב להפריד בין הודעה על מינוי לבין אישור סופי."
        outlook = "עדיין פתוח: אישור סופי, מועד כניסה לתפקיד, וקווי מדיניות שפורסמו."
    elif re.search(r"תייר|נופש|מלונ|חופשה|משרד התיירות", blob):
        why_matters = "הדיווח נוגע למחירים, זמינות או מדיניות שמשפיעים על מטיילים."
        background = "ענף התיירות רגיש לביטחון, מחירים ועונות חגים."
        outlook = "חסרים פרטי מחיר/זמינות והודעות רשמיות."
    elif re.search(r"ספורט|כדורגל|מכבי|הפועל|נבחרת|פרמייר|העברה|שחקנ", blob):
        why_matters = "הדיווח נוגע לשינוי בסגל או לתוצאה רשמית."
        background = "בספורט יש פער תכוף בין שמועה לאישור רשמי."
        outlook = "חסר אישור המועדון/הליגה או תוצאה רשמית."
    elif re.search(r"בורסה|ריבית|מני|דולר|השקע|בנק ישראל", blob):
        why_matters = "הדיווח נוגע לנתון או מהלך כלכלי שפורסם."
        background = "חשוב להבחין בין עובדה שפורסמה לבין פרשנות שוק."
        outlook = "חסרים נתון מאושר או הודעה רשמית מעודכנת."
    else:
        background, outlook, why_matters = _substantive_fallback(title, headlines, names)

    return {
        "title": title,
        "summary": summary,
        "why_matters": why_matters,
        "bullet_facts": bullets[:6],
        "background": background,
        "outlook": outlook,
        "insight": "",
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
    dropped = 0
    for item in items:
        scrubbed = scrub_item_sources(item)
        if not scrubbed:
            dropped += 1
            continue
        d = distill_item(scrubbed, cache=cache)
        enriched = dict(scrubbed)
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
    if dropped:
        print(f"[distill] dropped {dropped} opinion/noise clusters")
    save_cache(cache)
    return out
