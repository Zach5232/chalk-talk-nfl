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




def _debug_check_markets():
    """TEMPORARY diagnostic -- not part of the real pipeline, will be removed after use."""
    import urllib.request, urllib.error, json as _json
    events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/?apiKey={API_KEY}"
    with urllib.request.urlopen(events_url, timeout=15) as resp:
        events = _json.loads(resp.read())
        headers_seen = dict(resp.headers)
    print(f"Found {len(events)} real upcoming/live NFL events.")
    print(f"Real account quota -- used: {headers_seen.get('x-requests-used')}, remaining: {headers_seen.get('x-requests-remaining')}")
    if not events:
        print("No events available to test against right now.")
        return
    event_id = events[0]["id"]
    print(f"Testing against real event: {events[0]['away_team']} @ {events[0]['home_team']} ({event_id})")

    def try_market(market):
        url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds?apiKey={API_KEY}&regions=us&markets={market}&oddsFormat=american"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = _json.loads(resp.read())
                n_books = len(data.get("bookmakers", []))
                has_market = any(m["key"] == market for bm in data.get("bookmakers", []) for m in bm.get("markets", []))
                print(f"  {market:25s} -> HTTP 200, {n_books} bookmaker(s) responded, market actually present: {has_market}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(f"  {market:25s} -> HTTP {e.code}: {body}")
        except Exception as e:
            print(f"  {market:25s} -> ERROR: {e}")

    for market in ["spreads", "h2h", "totals", "team_totals", "alternate_spreads", "alternate_totals", "player_pass_yds"]:
        try_market(market)


if __name__ == "__main__":
    _debug_check_markets()  # TEMPORARY -- real call (capture(SEASON, WEEK)) restored after this check

