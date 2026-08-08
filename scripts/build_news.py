#!/usr/bin/env python3
"""Build news.json for GitHub Pages (no HTTP server)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve import build_payload  # noqa: E402


def main() -> None:
    payload = build_payload()
    out = ROOT / "news.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out.name} ({payload.get('count', 0)} items)")


if __name__ == "__main__":
    main()
