#!/usr/bin/env python3
"""Build news.json for GitHub Pages (no HTTP server)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve import build_payload  # noqa: E402

# Google News occasionally 503s every feed for a runner. Publishing that result
# would replace a good board with an empty one until the next build.
MIN_RATIO_OF_PREVIOUS = 0.4


def load_previous(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    # Ship the debug fields (ids + per-item/source diagnostics) so the gated
    # ?debug=1 explainability panel works on the public Pages site too.
    payload = build_payload(debug=True)
    out = ROOT / "news.json"
    count = int(payload.get("count") or 0)
    previous = load_previous(out)
    prev_count = int((previous or {}).get("count") or 0)

    if count == 0:
        if prev_count:
            print(
                f"refusing to overwrite {out.name}: built 0 items, "
                f"keeping previous {prev_count} items"
            )
            return 0
        print(f"built 0 items and no previous {out.name} to keep", file=sys.stderr)
        return 1

    if prev_count and count < prev_count * MIN_RATIO_OF_PREVIOUS:
        print(
            f"refusing to overwrite {out.name}: built {count} items vs "
            f"previous {prev_count} — likely a partial feed outage"
        )
        return 0

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out.name} ({count} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
