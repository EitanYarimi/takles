# תכל׳ס (ClearNews) — POC

פורטל חדשות ישראלי יבש: כותרת ראשית לישראל, כותרת בעולם, וריבועי נושאים.
לכל כתבה נוצר **סיכום מראש** (עובדות · רקע · מבט קדימה) בזמן משיכת הפיד.

## Live

`https://eitanyarimi.github.io/takles/`

## הרצה מקומית

```bash
# אופציונלי לזיקוק AI:
export GEMINI_API_KEY=AIza...
python3 serve.py
```

פתחו http://127.0.0.1:8765/

בלי מפתח — סיכום היוריסטי מראש (עדיין בלי פעולה מהמשתמש).
עם מפתח — Gemini; תוצאות נשמרות ב־`distill_cache.json`.

לבניית `news.json` ל־Pages:

```bash
python3 scripts/build_news.py
```
