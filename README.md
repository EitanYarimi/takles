# תכל׳ס (ClearNews) — POC

פורטל חדשות ישראלי יבש: כותרת ראשית לישראל, כותרת בעולם, וריבועי נושאים.

**השרת** מושך את הפידים, קורא גופי כתבות מהמקורות (פענוח Google News), מסנן דעות, ומזקק לכל כתבה: בדיקת אמינות · סיכום אחרי הצלבה · נתונים · רקע · מה עוד לא ברור.  
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

בלי מפתח — נמשכים גופי כתבות + סיכום היוריסטי (אמינות מסומנת כבדיקה חלקית). עם מפתח — Gemini מצליב מקורות וקובע `reliability`; תוצאות ב־`distill_cache.json` / `article_cache.json`.

ב־GitHub Actions יש להגדיר Secret בשם `GEMINI_API_KEY` כדי לקבל סיכום AI אמיתי ב־Pages.

**תקציר קולי:** מקומית Edge TTS דרך `/api/tts`. ב־Pages — הקראת דפדפן.

לבניית `news.json` ל־Pages:

```bash
python3 scripts/build_news.py
```
