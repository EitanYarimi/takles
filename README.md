# תכל׳ס (ClearNews) — POC

פורטל חדשות ישראלי יבש: כותרת ראשית לישראל, כותרת בעולם, וריבועי נושאים.

**השרת** מושך את הפידים, מסנן דעות, ומזקק לכל כתבה עובדות · ניתוח · רקע · מה עוד לא ברור.  
**הלקוח** רק מציג את ה־JSON המוכן — בלי משיכה/זיקוק בדפדפן.

## Live

`https://eitanyarimi.github.io/takles/`

ב־GitHub Pages הזיקוק רץ ב־GitHub Actions (`scripts/build_news.py` → `news.json`).

## הרצה מקומית

```bash
python3 -m pip install -r requirements.txt
# אופציונלי לזיקוק AI:
export GEMINI_API_KEY=AIza...
python3 serve.py
```

פתחו http://127.0.0.1:8765/

בלי מפתח — סיכום היוריסטי בשרת. עם מפתח — Gemini; תוצאות ב־`distill_cache.json`.

**תקציר קולי:** מקומית Edge TTS דרך `/api/tts`. ב־Pages — הקראת דפדפן.

לבניית `news.json` ל־Pages:

```bash
python3 scripts/build_news.py
```
