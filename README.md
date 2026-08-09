# תכל׳ס (ClearNews) — POC

פורטל חדשות ישראלי יבש: כותרת ראשית לישראל, כותרת בעולם, ריבועי נושאים, סיכום היום, ותובנות מדדיות.

**עקרון יסוד:** השרת מושך · קורא · מצליב · מזקק. הדפדפן רק מציג.

Live: [eitanyarimi.github.io/takles](https://eitanyarimi.github.io/takles/) · Repo: [EitanYarimi/takles](https://github.com/EitanYarimi/takles)

---

## Design principles

1. **Server digests, client renders** — אין משיכת RSS / פענוח Google News / זיקוק AI בדפדפן.
2. **עובדות לפני ספין** — מקבצי דעה/פרשנות מסוננים; כותרות דרמטיות מיושרות.
3. **הצלבת מקורות** — גופי כתבות נמשכים מהמוציאים לאור; סיכום אחרי בדיקת אמינות.
4. **בלי קישורים החוצה** — הכתבה נשארת בתוך תכל׳ס (מקורות מוצגים כשמות/כותרות).
5. **לוח חי היום** — כרטיסים פעילים לפי יום ישראל + אירועי ביטחון מתגלגלים; «צפיתי» מסיר מהלוח.
6. **שתי ריצות זהות בלוגיקה** — מקומית (`serve.py`) ו־Pages (`build_news.py`) משתמשות ב־`build_payload()` + `enrich_items()`.

---

## Architecture overview

```text
┌─────────────────────────────────────────────────────────────┐
│  Google News RSS (IL / World / Focus / Nation / Biz / …)    │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  serve.build_payload()                                      │
│    parse → filter → merge_items (dedupe)                    │
│    distill.enrich_items()                                   │
│      scrub opinions → hydrate articles → Gemini/heuristic   │
│    distill.build_daily_brief() → dailyBrief on payload      │
└────────────────────────────┬────────────────────────────────┘
                             ▼
              ┌──────────────┴──────────────┐
              ▼                             ▼
     Local serve.py                  GitHub Actions
     /api/news + WebSocket           news.json → Pages
              │                             │
              └──────────────┬──────────────┘
                             ▼
                    index.html (render only)
                    topics · daily brief · freshness · seen · detail · TTS
```

| Path | Entry | News data | Refresh | TTS |
|------|--------|-----------|---------|-----|
| **Local** | `python3 serve.py` → `http://127.0.0.1:8765/` | `GET /api/news` | WS every ~45s + HTTP poll 60s | Edge TTS `/api/tts` |
| **Pages** | static `_site/` | `./news.json` | HTTP poll every 5 min (rebuild is hourly) | Browser `speechSynthesis` |

זיהוי סביבה בלקוח: `IS_PAGES = /github\.io$/i.test(location.hostname)`.

---

## Data pipeline

1. **Fetch feeds** (`serve.safe_feed` / `parse_google`)
   - Main IL, World, World-focus (US/Europe/China/Ukraine search), Nation, Business, Sports, Travel/leisure
2. **Topic hints + filters** — business/leisure relevance gates; `topicHint` על פריטים
3. **`merge_items`** — איחוד והסרת כפילויות לפי כותרת מנורמלת
4. **`distill.enrich_items`**
   - `scrub_item_sources` — השארת מקורות חדשותיים; דחיית מקבצי דעה
   - `hydrate_item_sources` — פענוח `news.google.com/rss/articles/…` (`googlenewsdecoder`) + משיכת HTML → טקסט
   - `distill_item` — Gemini אם יש מפתח, אחרת heuristic; מטמון לפי fingerprint
   - סילוק `excerpt` מהמקורות לפני שליחה ללקוח (רק שם/כותרת/url)
5. **Payload** — `{ fetchedAt, count, withImages, items, dailyBrief }` → API או `news.json`

גרסת זיקוק נוכחית: **`DISTILL_VERSION = v11-flash-latest-throttle`**.

### Daily brief (`dailyBrief`)

נבנה בשרת אחרי `enrich_items()` מתוך פריטי היום (`Asia/Jerusalem`), מדורגים לפי חשיבות (ביטחון / רב־מקור / אמינות / רלוונטיות).

| Field | Meaning |
|-------|---------|
| `date` | תאריך ישראל `YYYY-MM-DD` |
| `headline` / `dek` | כותרת ותת־כותרת ל־UI |
| `points` | 4–7 נקודות יבשות (בלי קישורים) |
| `mode` | `gemini` או `heuristic` |
| `storyCount` | כמה סיפורים שימשו כמועמדים |

עם מפתח Gemini — סיכום AI קצר מהסיפורים המובילים; אחרת נקודות מ־`summary` / `bullet_facts` / כותרת.

---

## Modules

| File | Responsibility |
|------|----------------|
| `serve.py` | HTTP סטטי + `/api/news`, `/api/tts`, `/ws`; משיכת פידים; `build_payload()`; לולאת רענון |
| `distill.py` | סינון דעות, משיכת גופי כתבות, זיקוק Gemini/heuristic, `build_daily_brief`, caches |
| `scripts/build_news.py` | בנייה אופליין ל־Pages: `build_payload()` → `news.json` |
| `index.html` | UI בלבד: דירוג, נושאים, סיכום היום, TTL, נצפו, פירוט, תובנות, TTS |
| `insights.json` | פאנלים מדדיים קורטיים (למשל תפקוד ממשלה); מזג אוויר נמשך בלקוח (Open-Meteo) |
| `requirements.txt` | `edge-tts`, `googlenewsdecoder` |
| `.github/workflows/deploy-pages.yml` | cron כל שעה + push → build + deploy Pages |

מטמונים מקומיים (ב־`.gitignore`, לא ב־CI בין ריצות):

- `distill_cache.json` — תוצאות זיקוק לפי fingerprint
- `article_cache.json` — גופי כתבות (TTL ~36 שעות)

---

## Distill & reliability

### Modes

| Mode | When | Behavior |
|------|------|----------|
| `gemini` | `GEMINI_API_KEY` או `GOOGLE_API_KEY` מוגדר | קורא כותרות + גופי כתבות; מחזיר JSON עובדתי + אמינות |
| `heuristic` | אין מפתח / כשל API | סיכום מגופים/כותרות; אמינות לפי מספר גופים שנמשכו |

מודל ברירת מחדל: `GEMINI_MODEL=gemini-flash-latest` (למפתחות חדשים לרוב אין מכסת free-tier על `gemini-2.0-flash`).

### Fields per item (server → client)

| Field | Meaning |
|-------|---------|
| `status` | `confirmed` \| `reported` \| `denied` \| `review` |
| `reliability` | `high` \| `medium` \| `low` \| `unknown` |
| `reliability_notes` | הסבר קצר איך נקבעה האמינות |
| `digest_basis` | `fulltext` או `headlines` |
| `summary` | סיכום אחרי הצלבה |
| `bullet_facts` | 3–6 נתונים יבשים |
| `why_matters` | השלכה עובדתית (אופציונלי) |
| `background` / `outlook` | רקע · מה עוד לא ברור |
| `distillMode` | `gemini` \| `heuristic` |

### Limits (env-overridable)

| Variable | Default | Role |
|----------|---------|------|
| `DISTILL_MAX_DEEP` | `28` | כמה כתבות ראשונות מקבלות משיכת גוף |
| `DISTILL_MAX_SOURCES` | `3` | מקורות לכל כתבה |
| `DISTILL_ARTICLE_CHARS` | `3200` | אורך excerpt לזיקוק |
| Fetch timeout | 12s | HTTP למוציא לאור |
| Gemini timeout | 60s | קריאת API |

חלק מהאתרים מחזירים 403 — אז נשענים על מקורות אחרים במקבץ או על כותרות.

---

## Client UX

### Composition

- **Hero / lead** — כותרת ישראל + תקציר קצר
- **World lead** — כותרת בעולם
- **Topic cubes** — כניסה לנושא (ביטחון, פוליטיקה, אזור, בעולם, כלכלה, פנים, תיירות ונופש, ספורט)
- **Daily brief** — «סיכום היום»: 4–7 נקודות מהשרת (`dailyBrief`)
- **Insights** — מדדים קורטיים + מזג אוויר חי
- **Detail** — כרטיס מלא בלי יציאה לאתר חיצוני

### Detail sections (סדר)

1. בדיקת אמינות (+ badge)
2. סיכום אחרי הצלבה (+ השלכה)
3. נתונים
4. רקע
5. מה עוד לא ברור
6. כותרות מקורות

### Freshness & seen

- לוח פעיל: לא־נצפה **וגם** טרי (`isFreshOnBoard`)
- «היום» לפי `Asia/Jerusalem`, או ביטחון/אירוע מתגלגל תוך **12 שעות** (`ROLLING_TTL_H`)
- «צפיתי» → `localStorage` (`takles_seen_map`), TTL **7 ימים**

### Ranking / topics

- `classifyTopic` / `resolveTopic` — סיווג לפי כותרת + `topicHint` מהשרת
- דירוג רלוונטיות בלוח (ביטחון וכו׳); דמוט של לא־מאומת מחוץ לביטחון/אירועים מתגלגלים

### TTS

- מקומי: `he-IL-HilaNeural` דרך `/api/tts`
- Pages: `speechSynthesis` בדפדפן
- טקסט מתקציר: כותרת + סיכום + השלכה/outlook

---

## Deploy (GitHub Pages)

Workflow: `.github/workflows/deploy-pages.yml`

| Trigger | Schedule |
|---------|----------|
| `push` to `main` | מיידי |
| `schedule` | `0 * * * *` (top of every hour) |
| `workflow_dispatch` | ידני |

`news.json` rebuilds hourly on the schedule (and on every push). The client on Pages may still poll `./news.json` every 5 minutes; between rebuilds that poll just reloads the same file.

Steps: checkout → Python 3.12 → `pip install -r requirements.txt` → `scripts/build_news.py` (timeout 25m, `GEMINI_API_KEY` מ־Secrets) → stage `_site/` (`index.html`, `news.json`, `insights.json`, `media/`) → upload artifact → deploy Pages.

**חובה לסיכום AI ב־Pages:** Secret בשם `GEMINI_API_KEY`. בלי זה — hydrate של גופים + heuristic בלבד.

---

## Local run

```bash
python3 -m pip install -r requirements.txt
export GEMINI_API_KEY=AIza...   # אופציונלי אך מומלץ
python3 serve.py
```

פתחו http://127.0.0.1:8765/

בניית `news.json` כמו ב־CI:

```bash
python3 scripts/build_news.py
```

| Setting | Value |
|---------|-------|
| Bind | `127.0.0.1:8765` |
| Feed poll | every `POLL_SECONDS` (45) |
| Endpoints | `/`, `/api/news`, `/api/tts`, `/ws`, `/insights.json` |

---

## Intentionally not client-side

- משיכת RSS / Google News
- פענוח קישורי Google News ומשיכת גוף כתבה
- סינון דעות וזיקוק Gemini/heuristic
- כתיבת `news.json` או caches
- שליחת excerpts ללקוח

הלקוח **כן** עושה: סיווג נושא, דירוג תצוגה, freshness/seen, תמה, מזג אוויר לתובנות, הצגה ו־TTS.

---

## Repo layout

```text
clearnews-poc/
├── index.html                 # UI
├── serve.py                   # local server + shared build_payload
├── distill.py                 # scrub / hydrate / distill
├── scripts/build_news.py      # Pages news.json builder
├── insights.json              # curated insight panels
├── news.json                  # last built payload (Pages artifact source)
├── requirements.txt
├── media/                     # static assets
├── .github/workflows/deploy-pages.yml
└── README.md
```
