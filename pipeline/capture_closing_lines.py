"""
Capture the real, near-kickoff closing line -- best number across every book the Odds API
shows us -- for personal bet CLV (closing-line value) tracking.

This is deliberately SEPARATE from the Model Season Record (see build_model_season_record's
docstring in weekly_update.py): the model's own scoreboard grades against the PFR/nflverse
consensus close on purpose, because grading it against the best number across books would
quietly bake in a line-shopping edge the model doesn't structurally have. CLV -- "did I
personally beat the closing number" -- is a real, different question, and the honest answer
requires the ACTUAL best-of-market number, which only exists if captured close to kickoff
(the live odds board stops quoting a game entirely once it starts, which is exactly why a
run of the main weekly pipeline after the fact can't reconstruct this after the game is over).

Cost design: meant to run on a broad, frequent cron (every ~15 min across generous windows
around Thu/Sun/Mon kickoffs -- see .github/workflows/closing-line-capture.yml) so it doesn't
need to be precisely timed against ET/EDT-EST clock changes. Every invocation does one free
local schedule check first (real nflverse gameday/gametime, no API cost) and only calls the
paid Odds API -- one call total, regardless of how many games qualify -- when at least one
real game this week is kicking off within CAPTURE_WINDOW_MINUTES. Every other invocation
(the vast majority, given the broad cron) exits immediately with zero API cost, so a generous
cron schedule is safe regardless of Odds API plan/quota.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os

import pandas as pd

from weekly_update import (
    SEASON, WEEK, MODE, API_KEY, HIST_DATE,
    fetch_games_csv, pull_week_odds, REV_MAP, get_firestore_client, _FIREBASE_AVAILABLE,
)
if _FIREBASE_AVAILABLE:
    from weekly_update import firestore

CAPTURE_WINDOW_MINUTES = 45  # capture if kickoff is this soon or has *just* happened
GRACE_MINUTES_AFTER_KICKOFF = 25  # widened from 10 after a real audit found EVERY single
                                   # capture this season had missed -- GitHub Actions cron is
                                   # documented to run late under load, and NFL Sunday is
                                   # exactly the kind of shared high-load window that causes
                                   # it (a 15-min-interval cron delayed by only ~10-15 min,
                                   # which is common, blows straight through the old 10-min
                                   # grace). The book still quotes a frozen pre-game number
                                   # for a while after kickoff in practice, so this is a real
                                   # safety margin, not a meaningfully staler number.
EASTERN = ZoneInfo("America/New_York")


def games_kicking_off_soon(season, week):
    """Real nflverse schedule check, no API cost -- which of this week's real games are
    about to kick off (or just did)."""
    games = pd.read_csv(fetch_games_csv())
    g = games[(games.season.astype(str) == str(season)) & (games.week == week) & (games.game_type == "REG")]
    now = datetime.now(timezone.utc)
    soon = []
    for _, r in g.iterrows():
        if pd.isna(r.gameday) or pd.isna(r.gametime):
            continue
        try:
            kickoff_et = datetime.strptime(f"{r.gameday} {r.gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)
        except ValueError:
            continue
        minutes_away = (kickoff_et.astimezone(timezone.utc) - now).total_seconds() / 60.0
        if -GRACE_MINUTES_AFTER_KICKOFF <= minutes_away <= CAPTURE_WINDOW_MINUTES:
            soon.append({
                "id": f"{r.away_team.lower()}-{r.home_team.lower()}",
                "home_team": r.home_team, "away_team": r.away_team,
                "minutes_to_kickoff": round(minutes_away, 1),
            })
    return soon


def capture(season, week):
    targets = games_kicking_off_soon(season, week)
    if not targets:
        print(f"No {season} week {week} games kicking off within {CAPTURE_WINDOW_MINUTES} min "
              f"(or {GRACE_MINUTES_AFTER_KICKOFF} min past) -- skipping, no API call made.")
        return

    print(f"{len(targets)} game(s) in the capture window: " +
          ", ".join(f"{t['away_team']}@{t['home_team']} ({t['minutes_to_kickoff']:+.0f}m)" for t in targets))

    odds_data = pull_week_odds(MODE, API_KEY, HIST_DATE)

    cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    fdb = get_firestore_client(cred_path) if (_FIREBASE_AVAILABLE and cred_path) else None
    if not fdb:
        print("No Firestore credentials configured -- will print only, nothing written.")

    for t in targets:
        home_full, away_full = REV_MAP[t["home_team"]], REV_MAP[t["away_team"]]
        match = next((g for g in odds_data if g["home_team"] == home_full and g["away_team"] == away_full), None)
        if not match:
            print(f"  {t['id']}: no live odds available -- skipped")
            continue
        books = []
        for bm in match.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk["key"] != "spreads":
                    continue
                hp = next((oc["point"] for oc in mk["outcomes"] if oc["name"] == home_full), None)
                if hp is not None:
                    books.append({"book": bm["key"], "home_pt": hp})
        if not books:
            print(f"  {t['id']}: matched game but no spreads posted -- skipped")
            continue
        home_pts = [b["home_pt"] for b in books]
        best_home_pt = max(home_pts)   # best number for a HOME bettor (higher = fewer points needed / more given)
        best_away_pt = round(-min(home_pts), 2)  # best number for an AWAY bettor
        print(f"  {t['id']}: best_home_pt={best_home_pt} best_away_pt={best_away_pt} ({len(books)} books)")
        if fdb:
            fdb.collection("closing_lines").document(t["id"]).set({
                "books": books, "best_home_pt": best_home_pt, "best_away_pt": best_away_pt,
                "minutes_to_kickoff_at_capture": t["minutes_to_kickoff"],
                "captured_at": firestore.SERVER_TIMESTAMP,
            })




def _debug_opening_lines():
    """TEMPORARY -- removed after use."""
    """TEMPORARY diagnostic -- pulls REAL historical opening-ish line snapshots for several past
    2025 weeks, to compare against real closing lines already in games.csv. Prints only line data
    and quota usage, never the API key."""
    import json as _json2
    import urllib.request as _ur2

    # (week, opening_snapshot_iso) -- snapshot picked as 5 days before that week's earliest real
    # game, ~14:00 UTC (~10am ET), matching the real Tuesday-after-MNF pattern lines typically open.
    WEEKS = [
        (5,  "2025-09-27T14:00:00Z"),
        (6,  "2025-10-04T14:00:00Z"),
        (7,  "2025-10-11T14:00:00Z"),
        (8,  "2025-10-18T14:00:00Z"),
        (9,  "2025-10-25T14:00:00Z"),
        (10, "2025-11-01T14:00:00Z"),
        (11, "2025-11-08T14:00:00Z"),
        (12, "2025-11-15T14:00:00Z"),
        (13, "2025-11-22T14:00:00Z"),
        (14, "2025-11-29T14:00:00Z"),
    ]

    results = []
    for week, snap in WEEKS:
        url = (f"https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/odds/"
               f"?apiKey={API_KEY}&regions=us&markets=spreads&oddsFormat=american&date={snap}")
        try:
            with _ur2.urlopen(url, timeout=20) as resp:
                data = _json2.loads(resp.read())
                headers_seen = dict(resp.headers)
        except Exception as e:
            print(f"WEEK {week} @ {snap}: ERROR {e}")
            continue

        games = data.get("data", data) if isinstance(data, dict) else data
        print(f"WEEK {week} @ {snap}: {len(games)} games, quota used={headers_seen.get('x-requests-used')} remaining={headers_seen.get('x-requests-remaining')} (last cost={headers_seen.get('x-requests-last')})")
        for g in games:
            home, away = g.get("home_team"), g.get("away_team")
            best_home_pt = None
            pts = []
            for bm in g.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk["key"] != "spreads":
                        continue
                    hp = next((oc["point"] for oc in mk["outcomes"] if oc["name"] == home), None)
                    if hp is not None:
                        pts.append(hp)
            if pts:
                best_home_pt = sum(pts) / len(pts)  # simple average across books as "the opening number"
            print(f"  RESULT week={week} home={home} away={away} open_home_pt={best_home_pt} n_books={len(pts)}")


if __name__ == "__main__":
    _debug_opening_lines()  # TEMPORARY -- real call (capture(SEASON, WEEK)) restored after this check

