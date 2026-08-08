#!/usr/bin/env python3
"""ClearNews POC: static files, news API, and WebSocket push for headlines."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import ssl
import struct
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
PORT = 8765
POLL_SECONDS = 45
SSL_CTX = ssl._create_unverified_context()
UA = "Mozilla/5.0 ClearNewsPOC/0.4"
GOOGLE_RSS = "https://news.google.com/rss?hl=he&gl=IL&ceid=IL:he"
GOOGLE_SPORTS = (
    "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=he&gl=IL&ceid=IL:he"
)
# תיירות / נופש רלוונטיים — בלי בידור
GOOGLE_TRAVEL = (
    "https://news.google.com/rss/search?hl=he&gl=IL&ceid=IL:he&q="
    "%22%D7%AA%D7%99%D7%99%D7%A8%D7%95%D7%AA%22%20OR%20%22%D7%A0%D7%95%D7%A4%D7%A9%22"
    "%20OR%20%22%D7%9E%D7%9C%D7%95%D7%A0%D7%95%D7%AA%22%20OR%20%22%D7%97%D7%95%D7%A4%D7%A9%D7%94%22"
    "%20OR%20%22%D7%9E%D7%A9%D7%A8%D7%93%20%D7%94%D7%AA%D7%99%D7%99%D7%A8%D7%95%D7%AA%22"
)
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

SOURCE_RE = re.compile(
    r'<a href="([^"]+)"[^>]*>(.*?)</a>\s*&nbsp;&nbsp;<font[^>]*>(.*?)</font>',
    re.I | re.S,
)

LEISURE_KEEP = re.compile(
    r"משרד התיירות|תיירות ישראל|ענף התיירות|בתי.?המלון|מלונ|"
    r"נופש|חופשה|חבילת נופש|חדרי מלון|אתר נופש|למטייל|"
    r"ריזורט|דיל טיס|טיסת צ'?ארטר",
    re.I,
)
LEISURE_DROP = re.compile(
    r"דה\s*וויס|The Voice|סדרת|עונה \d|נטפליקס|קונצרט|זמר |בידור|"
    r"מכבי|הפועל|ספורט|ליגת|פרמייר|"
    r"איראן|הורמוז|מלחמ|כטב|רקטה|חיזבאללה|חמאס|עזה|"
    r"ביידן|טראמפ|בורסה|מני[וה]|אקזיט|טבק|עיקול|חילול שבת|"
    r"דונג נאי|מונדיאל 2030|מדד אינדקס|חיידק|טפיל|בוץ וסלעים|"
    r"רוסיה|אוקראינ|ים השחור|קאן טו|לייף סטייל|פיננסים וטכנול|"
    r"פורצת דרך|FlyAll|הונאות|תיירות אקולוגית|"
    r"מגזר התיירות של|מעלימה|קניון|גניב|"
    r"מינוי סמנכ",
    re.I,
)

_lock = threading.Lock()
_clients: set = set()
_latest_payload: dict | None = None
_latest_fp = ""


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def parse_google(xml: bytes, limit: int = 24, topic_hint: str | None = None) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for node in root.findall(".//item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        desc = node.findtext("description") or ""
        main_source = ""
        if " - " in title:
            title, main_source = title.rsplit(" - ", 1)
            title, main_source = title.strip(), main_source.strip()
        sources = []
        for m in SOURCE_RE.finditer(desc):
            sources.append(
                {
                    "url": html.unescape(m.group(1)),
                    "headline": strip_tags(m.group(2)),
                    "name": html.unescape(m.group(3)).strip(),
                }
            )
        if not sources:
            sources = [
                {
                    "url": link,
                    "headline": title,
                    "name": main_source or "Google News",
                }
            ]
        item = {
            "title": title,
            "link": link,
            "pubDate": pub,
            "sources": sources[:6],
            "image": None,
        }
        if topic_hint:
            item["topicHint"] = topic_hint
        items.append(item)
    return items


def is_relevant_leisure(item: dict) -> bool:
    title = item.get("title") or ""
    heads = " ".join(s.get("headline") or "" for s in item.get("sources") or [])
    blob = f"{title} {heads}"
    if LEISURE_DROP.search(blob):
        return False
    return bool(LEISURE_KEEP.search(blob))


def merge_items(*groups: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for group in groups:
        for item in group:
            key = re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def safe_feed(url: str, limit: int, topic_hint: str | None = None) -> list[dict]:
    try:
        items = parse_google(fetch(url), limit=limit, topic_hint=topic_hint)
        if topic_hint == "leisure":
            items = [it for it in items if is_relevant_leisure(it)]
        return items
    except Exception as exc:  # noqa: BLE001
        print(f"[poc] feed failed ({topic_hint or 'main'}): {exc}")
        return []


def build_payload() -> dict:
    main = safe_feed(GOOGLE_RSS, 20)
    sports = safe_feed(GOOGLE_SPORTS, 8, "sport")
    # מושכים יותר ואז מסננים לרלוונטי תיירות/נופש
    leisure = safe_feed(GOOGLE_TRAVEL, 25, "leisure")[:6]
    clusters = merge_items(main, sports, leisure)
    return {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(clusters),
        "withImages": 0,
        "items": clusters,
    }


def fingerprint(payload: dict) -> str:
    titles = [i.get("title", "") for i in payload.get("items", [])]
    return hashlib.sha1("|".join(titles).encode("utf-8")).hexdigest()


def ws_accept_key(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8") + WS_GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_send_text(conn, text: str) -> None:
    data = text.encode("utf-8")
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    conn.sendall(header + data)


def ws_recv_frames(conn):
    """Read frames; yield text payloads. Returns on close/error."""
    while True:
        hdr = conn.recv(2)
        if not hdr or len(hdr) < 2:
            return
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            ext = conn.recv(2)
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = conn.recv(8)
            length = struct.unpack("!Q", ext)[0]
        mask = conn.recv(4) if masked else b""
        payload = b""
        while len(payload) < length:
            chunk = conn.recv(length - len(payload))
            if not chunk:
                return
            payload += chunk
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:  # close
            return
        if opcode == 0x9:  # ping -> pong
            conn.sendall(bytes([0x8A, len(payload)]) + payload)
            continue
        if opcode == 0x1:
            yield payload.decode("utf-8", errors="replace")


def broadcast(payload: dict) -> None:
    msg = json.dumps({"type": "news", "payload": payload}, ensure_ascii=False)
    dead = []
    with _lock:
        clients = list(_clients)
    for conn in clients:
        try:
            ws_send_text(conn, msg)
        except Exception:  # noqa: BLE001
            dead.append(conn)
    if dead:
        with _lock:
            for conn in dead:
                _clients.discard(conn)
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


def refresh_news(force_broadcast: bool = False) -> dict | None:
    global _latest_payload, _latest_fp
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001
        print(f"[poc] news fetch failed: {exc}")
        return None
    fp = fingerprint(payload)
    with _lock:
        changed = fp != _latest_fp
        _latest_payload = payload
        _latest_fp = fp
    if changed or force_broadcast:
        broadcast(payload)
        print(f"[poc] push news ({payload['count']} items, changed={changed})")
    return payload


def poll_loop() -> None:
    while True:
        refresh_news(force_broadcast=False)
        time.sleep(POLL_SECONDS)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # POC: always serve fresh HTML so UI removals (accordion, refresh) show up
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/ws"):
            self._handle_ws()
            return
        if self.path.startswith("/api/news"):
            try:
                with _lock:
                    payload = _latest_payload
                if payload is None:
                    payload = refresh_news(force_broadcast=False) or {
                        "error": "no data",
                        "items": [],
                    }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return
        return super().do_GET()

    def end_headers(self):
        # POC: always serve fresh HTML/JS so UI removals (e.g. seen accordion) show up
        if self.path in ("/", "/index.html") or self.path.endswith(".html"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _handle_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "Missing Sec-WebSocket-Key")
            return
        accept = ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        conn = self.connection
        with _lock:
            _clients.add(conn)
            payload = _latest_payload
        print(f"[poc] ws client +1 (total {len(_clients)})")
        try:
            if payload is None:
                payload = refresh_news(force_broadcast=False)
            if payload:
                ws_send_text(
                    conn,
                    json.dumps({"type": "news", "payload": payload}, ensure_ascii=False),
                )
            # Keep alive until client disconnects / close frame
            for _ in ws_recv_frames(conn):
                pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _lock:
                _clients.discard(conn)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            print(f"[poc] ws client -1 (total {len(_clients)})")

    def log_message(self, fmt, *args):
        print(f"[poc] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    refresh_news(force_broadcast=False)
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"תכלס POC → http://127.0.0.1:{PORT}/")
    print(f"WebSocket → ws://127.0.0.1:{PORT}/ws (push every {POLL_SECONDS}s)")
    server.serve_forever()
