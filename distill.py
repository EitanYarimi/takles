#!/usr/bin/env python3
"""Pre-distill news items: facts, background, outlook (Gemini + heuristic fallback).

Reads publisher article bodies (via Google News URL decode), then asks Gemini
for a cross-source factual summary and reliability check.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "distill_cache.json"
ARTICLE_CACHE_PATH = ROOT / "article_cache.json"
DISTILL_VERSION = "v12-quota-circuit-cache"
SSL_CTX = ssl._create_unverified_context()
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
MAX_DEEP_ITEMS = int(os.environ.get("DISTILL_MAX_DEEP", "28"))
MAX_SOURCES_FETCH = int(os.environ.get("DISTILL_MAX_SOURCES", "3"))
MAX_ARTICLE_CHARS = int(os.environ.get("DISTILL_ARTICLE_CHARS", "3200"))
MAX_GEMINI_ITEMS = int(os.environ.get("DISTILL_MAX_GEMINI", "10"))
GEMINI_MIN_INTERVAL = float(os.environ.get("DISTILL_GEMINI_INTERVAL", "2.0"))
ARTICLE_CACHE_TTL = 60 * 60 * 36
FETCH_TIMEOUT = 12
_ARTICLE_LOCK = threading.Lock()
_LAST_GEMINI_TS = 0.0
_GEMINI_CIRCUIT_OPEN = False
_GEMINI_CONSEC_429 = 0
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
קיבלת כותרת מקבץ + קטעי גוף כתבה ממקורות (אחרי פענוח קישורי Google News), או כותרות בלבד אם אין גוף.
תפקידך: לקרוא את המקורות, להצליב נתונים, ולכתוב סיכום יבש אחרי בדיקת אמינות.

החזר JSON בלבד (בלי markdown) עם השדות:
- title: כותרת יבשה בעברית, בלי דרמה/קליקבייט/ציטוטי ספין
- bullet_facts: מערך 3–6 נתונים שנגזרים מגופי הכתבות/כותרות בלבד. אסור דעה/פרשנות. ציטוט פוליטי = רק "מי אמר/הגיב למי" אם זה הדיווח.
- summary: סיכום שלך אחרי הצלבה — 2–4 משפטים: מה חוזר בין מקורות, מה דיווח יחיד, איפה יש סתירה או חוסר. בלי דעה.
- reliability: high | medium | low | unknown
  high = לפחות שני מקורות עצמאיים על אותה עובדה ליבה, בלי סתירה מהותית
  medium = יש גוף/מקורות אבל אימות חלקי או פרטים שנויים במחלוקת
  low = מקור יחיד, או סתירות, או בעיקר ציטוט/ספין בלי נתון
  unknown = אין גוף כתבה / לא ניתן לבדוק
- reliability_notes: משפט–שניים בעברית: איך הגעת לרמת האמינות (מה הוצלב, מה חסר)
- why_matters: השלכה עובדתית אחת שנגזרת מהנתונים, או מחרוזת ריקה
- background: הקשר עובדתי קצר ורק אם נתמך; אחרת "חסר הקשר מצולב בקלט"
- outlook: מה עוד לא ברור לאימות (לא תחזית)
- status: confirmed | reported | denied | review

כללים:
- אל תמציא. אל תבחר צד. אל תשתמש במילות דרמה.
- הסתמך קודם על גופי הכתבות; כותרות רק כגיבוי.
- confirmed רק עם לפחות שני מקורות על אותה עובדה בלי לשון ספק.
"""


def load_article_cache() -> dict:
    try:
        if ARTICLE_CACHE_PATH.exists():
            return json.loads(ARTICLE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_article_cache(cache: dict) -> None:
    try:
        now = int(time.time())
        pruned = {
            k: v
            for k, v in cache.items()
            if isinstance(v, dict) and now - int(v.get("_ts") or 0) < ARTICLE_CACHE_TTL
        }
        if len(pruned) > 500:
            keys = sorted(pruned.keys(), key=lambda k: pruned[k].get("_ts", 0), reverse=True)
            pruned = {k: pruned[k] for k in keys[:400]}
        ARTICLE_CACHE_PATH.write_text(json.dumps(pruned, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[distill] article cache save failed: {exc}")


def resolve_publisher_url(url: str) -> str | None:
    u = (url or "").strip()
    if not u:
        return None
    if "news.google.com" not in u:
        return u
    try:
        from googlenewsdecoder import gnewsdecoder

        result = gnewsdecoder(u, interval=0)
        if isinstance(result, dict) and result.get("status") and result.get("decoded_url"):
            return str(result["decoded_url"]).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[distill] gnews decode failed: {exc}")
    return None


def html_to_text(html: str) -> str:
    raw = html or ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?is)</?(br|p|div|li|h[1-6]|tr|section|article)[^>]*>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = unescape(raw)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if len(s) < 40:
            continue
        # drop common chrome
        if re.search(r"cookie|מדיניות הפרטיות|תנאי שימוש|כל הזכויות|פרסומת|subscribe", s, re.I):
            continue
        lines.append(s)
    return "\n".join(lines).strip()


def fetch_article_text(url: str) -> str:
    u = (url or "").strip()
    if not u.startswith("http"):
        return ""
    req = urllib.request.Request(
        u,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=SSL_CTX) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read()
        if "html" not in ctype and not raw[:200].lstrip().lower().startswith(b"<!doctype"):
            # still try decode if looks like html
            if b"<html" not in raw[:800].lower() and b"<body" not in raw[:800].lower():
                return ""
        html = raw.decode("utf-8", "replace")
        text = html_to_text(html)
        if len(text) < 180:
            return ""
        return text[: MAX_ARTICLE_CHARS + 400]
    except Exception as exc:  # noqa: BLE001
        print(f"[distill] fetch failed {u[:60]}: {exc}")
        return ""


def _fetch_one_source(src: dict, article_cache: dict) -> dict:
    out = dict(src)
    url = (src.get("url") or "").strip()
    if not url:
        out["fetch_ok"] = False
        return out
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    now = int(time.time())
    with _ARTICLE_LOCK:
        hit = article_cache.get(key)
        if (
            isinstance(hit, dict)
            and hit.get("text")
            and now - int(hit.get("_ts") or 0) < ARTICLE_CACHE_TTL
        ):
            out["resolved_url"] = hit.get("resolved_url") or url
            out["excerpt"] = str(hit["text"])[:MAX_ARTICLE_CHARS]
            out["fetch_ok"] = True
            return out

    resolved = resolve_publisher_url(url) or ""
    text = fetch_article_text(resolved) if resolved else ""
    if text:
        with _ARTICLE_LOCK:
            article_cache[key] = {
                "resolved_url": resolved,
                "text": text[:MAX_ARTICLE_CHARS],
                "_ts": now,
            }
        out["resolved_url"] = resolved
        out["excerpt"] = text[:MAX_ARTICLE_CHARS]
        out["fetch_ok"] = True
    else:
        out["resolved_url"] = resolved or url
        out["excerpt"] = ""
        out["fetch_ok"] = False
    return out


def hydrate_item_sources(item: dict, article_cache: dict, deep: bool) -> dict:
    """Resolve Google News links and attach article excerpts when deep=True."""
    out = dict(item)
    sources = list(out.get("sources") or [])
    if not deep or not sources:
        out["digest_basis"] = "headlines"
        out["sources"] = sources
        return out

    jobs = sources[:MAX_SOURCES_FETCH]
    rest = sources[MAX_SOURCES_FETCH:]
    fetched: list[dict] = [dict(s) for s in jobs]
    with ThreadPoolExecutor(max_workers=min(4, len(jobs) or 1)) as pool:
        futs = {pool.submit(_fetch_one_source, s, article_cache): i for i, s in enumerate(jobs)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                fetched[i] = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[distill] source hydrate error: {exc}")
                fetched[i] = {**jobs[i], "fetch_ok": False, "excerpt": ""}

    out["sources"] = fetched + [dict(s) for s in rest]
    ok = sum(1 for s in fetched if s.get("fetch_ok") and s.get("excerpt"))
    out["digest_basis"] = "fulltext" if ok else "headlines"
    out["_fetched_count"] = ok
    return out


def _reliability_fallback(item: dict) -> tuple[str, str]:
    sources = item.get("sources") or []
    fetched = [s for s in sources if s.get("fetch_ok") and s.get("excerpt")]
    names = []
    for s in fetched:
        n = (s.get("name") or "").strip()
        if n and n not in names:
            names.append(n)
    if len(fetched) >= 2:
        return (
            "medium",
            f"נמשכו גופי כתבה מ־{len(fetched)} מקורות ({', '.join(names[:3])}), "
            "אך בלי בדיקת AI — מפתח Gemini חסר, מכסת API מלאה, או שהקריאה נכשלה.",
        )
    if len(fetched) == 1:
        return (
            "low",
            "נמשך גוף כתבה ממקור יחיד בלבד; בלי הצלבה מול AI אי אפשר לאשר אמינות גבוהה.",
        )
    return (
        "unknown",
        "לא נמשכו גופי כתבות מהמקורות; הסיכום מבוסס כותרות בלבד, בלי בדיקת אמינות AI.",
    )


def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def _gemini_available() -> bool:
    return bool(_api_key()) and not _GEMINI_CIRCUIT_OPEN


def _open_gemini_circuit(reason: str) -> None:
    global _GEMINI_CIRCUIT_OPEN
    if _GEMINI_CIRCUIT_OPEN:
        return
    _GEMINI_CIRCUIT_OPEN = True
    print(f"[distill] gemini circuit OPEN — skipping further AI calls this build ({reason})")


def _gemini_generate(prompt_body: dict, *, retries: int = 2) -> dict | None:
    """Shared Gemini generateContent with throttle + quota circuit breaker."""
    global _LAST_GEMINI_TS, _GEMINI_CONSEC_429
    if not _gemini_available():
        return None
    key = _api_key()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}"
    )
    payload = json.dumps(prompt_body).encode("utf-8")
    for attempt in range(max(1, retries)):
        wait = GEMINI_MIN_INTERVAL - (time.time() - _LAST_GEMINI_TS)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        try:
            _LAST_GEMINI_TS = time.time()
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            _GEMINI_CONSEC_429 = 0
            return raw
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 429:
                _GEMINI_CONSEC_429 += 1
                hard = bool(
                    re.search(r"limit:\s*0\b", detail, re.I)
                    or re.search(r"quota exceeded", detail, re.I)
                )
                # Hard daily/plan quota → stop burning the build; soft RPM → brief retry
                if hard or _GEMINI_CONSEC_429 >= 2:
                    _open_gemini_circuit(f"429 after {_GEMINI_CONSEC_429}: {detail[:120]}")
                    return None
                if attempt + 1 < retries:
                    retry_after = 6.0 * (attempt + 1)
                    m = re.search(r"retry in ([0-9.]+)s", detail, re.I)
                    if m:
                        retry_after = min(40.0, float(m.group(1)) + 0.5)
                    print(f"[distill] gemini 429, retry in {retry_after:.1f}s")
                    time.sleep(retry_after)
                    continue
                print(f"[distill] gemini failed: {exc} {detail[:180]}")
                return None
            print(f"[distill] gemini failed: {exc} {detail[:180]}")
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[distill] gemini failed: {exc}")
            return None
    return None


def dry_title(title: str) -> str:
    t = title or ""
    t = KICKER_RE.sub("", t)
    for w in DRAMATIC:
        t = t.replace(w, "")
    t = re.sub(r"\s{2,}", " ", t).strip(" -:·|\"״„“”?")
    return t or (title or "").strip()


def item_fingerprint(item: dict) -> str:
    parts = [DISTILL_VERSION, item.get("title") or "", item.get("digest_basis") or ""]
    for s in item.get("sources") or []:
        parts.append(s.get("name") or "")
        parts.append(s.get("headline") or "")
        excerpt = (s.get("excerpt") or "")[:240]
        if excerpt:
            parts.append(hashlib.sha1(excerpt.encode("utf-8")).hexdigest()[:12])
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

    reliability, reliability_notes = _reliability_fallback(item)
    basis = item.get("digest_basis") or "headlines"
    if basis == "fulltext" and any(s.get("excerpt") for s in sources):
        # Prefer short extracts from bodies over headline-only narrative when available
        body_bits = []
        for s in sources:
            ex = (s.get("excerpt") or "").strip()
            if not ex:
                continue
            first = re.split(r"[\n\.!?]", ex)[0].strip()
            if len(first) > 40:
                body_bits.append(f"{s.get('name') or 'מקור'}: {first[:140]}")
            if len(body_bits) >= 2:
                break
        if body_bits:
            summary = (
                f"על פי {len(body_bits)} מקורות שנקראו במלואם: "
                + " · ".join(body_bits)
                + "."
            )

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
        "reliability": reliability,
        "reliability_notes": reliability_notes,
        "digest_basis": basis,
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
    if not _gemini_available():
        return None
    sources = item.get("sources") or []
    lines = [
        f"כותרת מקבץ: {item.get('title') or ''}",
        f"בסיס קלט: {item.get('digest_basis') or 'headlines'}",
        "",
    ]
    for s in sources[:MAX_SOURCES_FETCH]:
        name = s.get("name") or "מקור"
        head = s.get("headline") or ""
        lines.append(f"### {name}")
        lines.append(f"כותרת: {head}")
        excerpt = (s.get("excerpt") or "").strip()
        if excerpt:
            lines.append("גוף כתבה (מקוצר):")
            lines.append(excerpt[:MAX_ARTICLE_CHARS])
        else:
            lines.append("(אין גוף כתבה — כותרת בלבד)")
        lines.append("")
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": PROMPT + "\n\n" + "\n".join(lines)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
    }
    raw = _gemini_generate(body, retries=2)
    if not raw:
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
    reliability = str(data.get("reliability") or "unknown").strip().lower()
    if reliability not in {"high", "medium", "low", "unknown"}:
        reliability = "unknown"
    reliability_notes = str(data.get("reliability_notes") or "").strip()
    if not reliability_notes:
        reliability_notes = {
            "high": "הצלבה חיובית בין מקורות על ליבת העובדה.",
            "medium": "אימות חלקי — יש גוף/מקורות אך לא אישור מלא.",
            "low": "אמינות נמוכה: מקור יחיד או סתירות/ספין.",
            "unknown": "לא ניתן לבדוק אמינות מהקלט שסופק.",
        }[reliability]
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
        "reliability": reliability,
        "reliability_notes": reliability_notes,
        "digest_basis": item.get("digest_basis") or "headlines",
        "mode": "gemini",
    }


def distill_item(item: dict, cache: dict | None = None, *, use_gemini: bool = True) -> dict:
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
        and hit.get("reliability")
        and hit.get("reliability_notes")
    ):
        out = {k: v for k, v in hit.items() if not k.startswith("_")}
        return out

    distilled = None
    if use_gemini:
        distilled = gemini_distill(item)
    if not distilled:
        distilled = heuristic_distill(item)
    cache[fp] = {**distilled, "_ts": int(time.time())}
    save_cache(cache)
    return distilled


def enrich_items(items: list[dict]) -> list[dict]:
    cache = load_cache()
    article_cache = load_article_cache()
    out = []
    dropped = 0
    deep_ok = 0
    gemini_ok = 0
    gemini_attempts = 0
    for idx, item in enumerate(items):
        scrubbed = scrub_item_sources(item)
        if not scrubbed:
            dropped += 1
            continue
        deep = idx < MAX_DEEP_ITEMS
        hydrated = hydrate_item_sources(scrubbed, article_cache, deep=deep)
        if hydrated.get("_fetched_count"):
            deep_ok += 1
        use_gemini = gemini_attempts < MAX_GEMINI_ITEMS and _gemini_available()
        if use_gemini:
            gemini_attempts += 1
        d = distill_item(hydrated, cache=cache, use_gemini=use_gemini)
        if d.get("mode") == "gemini":
            gemini_ok += 1
        enriched = dict(hydrated)
        enriched.pop("_fetched_count", None)
        # Don't ship huge excerpts to the client
        slim_sources = []
        for s in enriched.get("sources") or []:
            slim = {
                "url": s.get("url") or "",
                "headline": s.get("headline") or "",
                "name": s.get("name") or "",
            }
            if s.get("resolved_url"):
                slim["resolved_url"] = s.get("resolved_url")
            if s.get("fetch_ok"):
                slim["fetch_ok"] = True
            slim_sources.append(slim)
        enriched["sources"] = slim_sources
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
        enriched["reliability"] = d.get("reliability")
        enriched["reliability_notes"] = d.get("reliability_notes")
        enriched["digest_basis"] = d.get("digest_basis") or hydrated.get("digest_basis")
        enriched["distillMode"] = d.get("mode")
        out.append(enriched)
    if dropped:
        print(f"[distill] dropped {dropped} opinion/noise clusters")
    print(
        f"[distill] fulltext hydrate on {deep_ok}/{len(out)} kept items "
        f"(cap {MAX_DEEP_ITEMS}); gemini {gemini_ok}/{gemini_attempts} "
        f"(cap {MAX_GEMINI_ITEMS}, model {GEMINI_MODEL})"
    )
    save_cache(cache)
    save_article_cache(article_cache)
    return out


IL_TZ = ZoneInfo("Asia/Jerusalem")
DAILY_BRIEF_MIN = 4
DAILY_BRIEF_MAX = 7
DAILY_BRIEF_CANDIDATES = 8

_TOPIC_SCORE = {
    "security": 100,
    "region": 86,
    "world": 68,
    "politics": 54,
    "economy": 40,
    "domestic": 22,
    "leisure": 12,
    "sport": 14,
}

_SECURITY_RE = re.compile(
    r"חיזבאללה|לבנון|רחפן|צה[\"׳']?ל|יירוט|כיפת ברזל|חמאס|עזה|"
    r"פיקוד העורף|מחבל|חטופ|שבוי|רקטות?|אזעק|הורמוז|איראן|מלחמ|טיל",
    re.I,
)
_REGION_RE = re.compile(
    r"סוריה|עיראק|ירדן|מצרים|סעודיה|קטאר|עומאן|תימן|חתי|רפיח|גבול הצפון",
    re.I,
)
_WORLD_RE = re.compile(
    r"ארה[\"״]?ב|וושינגטון|אירופה|סין|רוסיה|אוקראינה|נאט[\"״]?ו|טראמפ|ביידן",
    re.I,
)
_POLITICS_RE = re.compile(
    r"כנסת|ממשלה|שר ה|ראש הממשלה|בחירות|קואליציה|אופוזיציה|חוק",
    re.I,
)
_ECONOMY_RE = re.compile(
    r"בורסה|ריבית|אינפלצי|דולר|שקל|מס|תקציב|משק|כלכל",
    re.I,
)

DAILY_BRIEF_PROMPT = """אתה עורך חדשות יבש בעברית לתכל׳ס.
כתוב סיכום יומי קצר של הדברים החשובים ביותר שהיום — עובדות בלבד.
כללים:
- 4 עד 7 נקודות קצרות (משפט אחד לכל נקודה).
- בלי דרמה, בלי דעה, בלי פרשנות, בלי אימוג׳י.
- בלי קישורים ובלי שמות אתרים.
- העדף ביטחון, אירועים רב־מקוריים ואמינות גבוהה.
- כל נקודה = עובדה אחת ברורה מהסיפורים שסופקו.
החזר JSON בלבד:
{"points":["...","..."]}
"""


def _parse_item_time(item: dict) -> datetime | None:
    raw = (item.get("pubDate") or item.get("published") or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_today_israel(item: dict, *, now: datetime | None = None) -> bool:
    dt = _parse_item_time(item)
    if not dt:
        return False
    now = now or datetime.now(IL_TZ)
    return dt.astimezone(IL_TZ).date() == now.date()


def _item_text_blob(item: dict) -> str:
    parts = [item.get("dryTitle") or "", item.get("title") or "", item.get("summary") or ""]
    for s in item.get("sources") or []:
        parts.append(s.get("headline") or "")
        parts.append(s.get("name") or "")
    for b in item.get("bullet_facts") or []:
        parts.append(str(b))
    return " ".join(parts)


def _infer_topic(item: dict) -> str:
    hint = (item.get("topicHint") or "").strip()
    if hint in _TOPIC_SCORE:
        return hint
    blob = _item_text_blob(item)
    if _SECURITY_RE.search(blob):
        return "security"
    if re.search(r"ספורט|כדורגל|כדורסל|NBA|FIFA", blob, re.I):
        return "sport"
    if re.search(r"תיירות|נופש|מלונ", blob) and not _SECURITY_RE.search(blob):
        return "leisure"
    if _ECONOMY_RE.search(blob) or hint == "economy":
        return "economy"
    if _POLITICS_RE.search(blob):
        return "politics"
    if _REGION_RE.search(blob):
        return "region"
    if _WORLD_RE.search(blob) or hint == "world":
        return "world"
    return "domestic"


def _reliability_factor(item: dict) -> float:
    rel = str(item.get("reliability") or "").lower()
    if rel == "high":
        return 1.2
    if rel == "medium":
        return 1.05
    if rel == "low":
        return 0.7
    status = str(item.get("status") or "reported")
    topic = _infer_topic(item)
    allow = topic == "security"
    if status == "confirmed":
        return 1.15
    if status == "denied":
        return 0.75 if allow else 0.55
    if status == "review":
        return 1.05 if allow else 0.42
    if status == "reported":
        return 1.0 if allow else 0.55
    return 0.9 if allow else 0.55


def _importance_score(item: dict, *, now: datetime | None = None) -> float:
    now = now or datetime.now(IL_TZ)
    dt = _parse_item_time(item)
    if dt:
        age_h = max(0.0, (now - dt.astimezone(IL_TZ)).total_seconds() / 3600.0)
    else:
        age_h = 12.0
    today = _is_today_israel(item, now=now)
    recency = (2.718281828 ** (-age_h / 10.0)) if today else (2.718281828 ** (-age_h / 4.0)) * 0.12
    sources_n = len(item.get("sources") or [])
    topic = _infer_topic(item)
    topic_score = _TOPIC_SCORE.get(topic, 14)
    boost = 0.0
    blob = _item_text_blob(item)
    if _SECURITY_RE.search(blob):
        boost += 36
    if sources_n >= 3:
        boost += 18
    elif sources_n >= 2:
        boost += 10
    if str(item.get("digest_basis") or "") == "fulltext":
        boost += 8
    relevance = topic_score + sources_n * 6 + boost
    return relevance * recency * _reliability_factor(item)


def _short_point(text: str, max_len: int = 140) -> str:
    raw = dry_title(re.sub(r"\s+", " ", str(text or "")).strip())
    if not raw:
        return ""
    sentence = re.split(r"(?<=[.!?…])\s+", raw)[0] or raw
    sentence = sentence.strip(" -:·|")
    if len(sentence) <= max_len:
        return sentence
    return sentence[: max_len - 1].rsplit(" ", 1)[0] + "…"


def _heuristic_daily_points(items: list[dict]) -> list[str]:
    points: list[str] = []
    seen: set[str] = set()
    for item in items:
        candidates = []
        bullets = item.get("bullet_facts") or []
        if bullets:
            candidates.append(str(bullets[0]))
        if item.get("summary"):
            # Skip meta "no AI" / raw body dumps in daily points
            s = str(item.get("summary"))
            if not re.search(r"בלי AI|על פי \d+ מקורות שנקראו", s):
                candidates.append(s)
        candidates.append(str(item.get("dryTitle") or item.get("title") or ""))
        point = ""
        for c in candidates:
            point = _short_point(c)
            if point:
                break
        if not point:
            continue
        key = re.sub(r"\W+", "", point.lower())[:48]
        if key in seen:
            continue
        seen.add(key)
        points.append(point)
        if len(points) >= DAILY_BRIEF_MAX:
            break
    return points


def _gemini_daily_points(items: list[dict]) -> list[str] | None:
    if not _gemini_available() or not items:
        return None
    lines = ["סיפורים לדירוג (מהחשוב לפחות):", ""]
    for idx, item in enumerate(items, 1):
        topic = _infer_topic(item)
        title = item.get("dryTitle") or item.get("title") or ""
        summary = (item.get("summary") or "")[:280]
        bullets = item.get("bullet_facts") or []
        sources_n = len(item.get("sources") or [])
        lines.append(f"{idx}. [{topic}] {title}")
        lines.append(
            f"   אמינות={item.get('reliability') or 'unknown'} · "
            f"סטטוס={item.get('status') or 'reported'} · מקורות={sources_n}"
        )
        if summary:
            lines.append(f"   סיכום: {summary}")
        if bullets:
            lines.append("   נתונים: " + " · ".join(str(b) for b in bullets[:3]))
        lines.append("")
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": DAILY_BRIEF_PROMPT + "\n\n" + "\n".join(lines)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    raw = _gemini_generate(body, retries=2)
    if not raw:
        return None
    try:
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    data = _extract_json(text)
    if not data:
        return None
    points = data.get("points") or data.get("bullets") or []
    if isinstance(points, str):
        points = [points]
    cleaned = []
    for p in points:
        s = _short_point(str(p), max_len=160)
        if s:
            cleaned.append(s)
    if len(cleaned) < DAILY_BRIEF_MIN:
        return None
    return cleaned[:DAILY_BRIEF_MAX]


def build_daily_brief(items: list[dict]) -> dict:
    """Rank today's stories and produce a short factual daily brief."""
    now = datetime.now(IL_TZ)
    today_items = [it for it in items if _is_today_israel(it, now=now)]
    pool = today_items if today_items else list(items)
    ranked = sorted(pool, key=lambda it: _importance_score(it, now=now), reverse=True)
    top = ranked[:DAILY_BRIEF_CANDIDATES]

    mode = "heuristic"
    points = _gemini_daily_points(top) if top and _gemini_available() else None
    if points:
        mode = "gemini"
    else:
        points = _heuristic_daily_points(top)

    points = [p for p in (points or []) if p][:DAILY_BRIEF_MAX]
    print(
        f"[distill] daily brief: {len(points)} points "
        f"(mode={mode}, today={len(today_items)}/{len(items)}, candidates={len(top)})"
    )
    return {
        "date": now.date().isoformat(),
        "headline": "סיכום היום",
        "dek": "הדברים הכי חשובים שהיום",
        "points": points,
        "mode": mode,
        "storyCount": len(top),
    }
