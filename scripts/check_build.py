#!/usr/bin/env python3
"""Ad-hoc build inspector: prints digest quality metrics without writing news.json."""
from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve import build_payload  # noqa: E402


def main() -> None:
    payload = build_payload()
    items = payload.get("items", [])
    print("=== items:", len(items))
    print("reliability :", dict(collections.Counter(i.get("reliability") for i in items)))
    print("status      :", dict(collections.Counter(i.get("status") for i in items)))
    print("mode        :", dict(collections.Counter(i.get("distillMode") for i in items)))

    bad = [
        i
        for i in items
        if i.get("status") == "confirmed" and (i.get("verification") or {}).get("bodies", 0) < 2
    ]
    print("BAD confirmed <2 bodies:", len(bad))
    short = [i for i in items if len(i.get("title") or "") < 18]
    print("titles < 18 chars      :", len(short), [(i.get("title") or "")[:22] for i in short[:5]])

    brief = payload.get("dailyBrief") or {}
    print(f"\n--- dailyBrief ({brief.get('mode')}) ---")
    for point in brief.get("points") or []:
        print("  -", point)

    print("\n--- cross-checked stories ---")
    for item in items:
        verification = item.get("verification") or {}
        if verification.get("bodies", 0) >= 2:
            print(
                f"[{item.get('reliability')}] ratio={verification.get('overlap_ratio')}",
                (item.get("title") or "")[:58],
            )
            print("   ", (item.get("reliability_notes") or "")[:160])


if __name__ == "__main__":
    main()
