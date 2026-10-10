"""Writes key_numbers.json (repo root, served next to ChalkTalk.html) -- the same score/margin
distributions the line shopper prices with (weekly_update._pmf), precomputed on a half-point grid of
market centers, so the dashboard's "check a price" box can price ANY line exactly the way the
pipeline does, with no odds pull. Free: built from nflverse games.csv. Re-run once a season or so:

    python pipeline/build_key_numbers.py
"""
import json, os
import numpy as np

import weekly_update as wu

GRIDS = {                       # market centers to precompute (half-point steps)
    "total": (30.0, 64.0),      # game total
    "team_total": (8.0, 38.0),  # one team's points
    "spread": (-21.0, 21.0),    # home margin (nflverse spread_line: > 0 = home favored)
}
MIN_P = 0.0002                  # drop outcomes rarer than this; the page treats them as 0


def main():
    out = {"_note": "P(exact outcome) by market center; built by pipeline/build_key_numbers.py", "pmf": {}}
    for kind, (lo, hi) in GRIDS.items():
        out["pmf"][kind] = {}
        for c in np.arange(lo, hi + 0.01, 0.5):
            pmf = wu._pmf(kind, float(c))
            out["pmf"][kind][f"{c:.1f}"] = {str(int(k)): round(float(p), 4) for k, p in pmf.items() if p >= MIN_P}
    path = os.path.join(os.path.dirname(wu.PIPELINE_DIR), "key_numbers.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {path} ({os.path.getsize(path) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
