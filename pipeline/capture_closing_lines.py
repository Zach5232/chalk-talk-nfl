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




def _debug_prop_consensus():
    """TEMPORARY -- removed after use."""
    import json, urllib.request
    """TEMPORARY diagnostic -- tests real multi-book player prop devig/consensus math against real
    live data. Prints only computed results, never the API key."""
    MARKETS = "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_pass_tds"

    def american_to_prob(odds):
        odds = float(odds)
        if odds < 0:
            return -odds / (-odds + 100)
        return 100 / (odds + 100)

    events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/?apiKey={API_KEY}"
    with urllib.request.urlopen(events_url, timeout=20) as resp:
        events = json.loads(resp.read())
    print(f"{len(events)} real events available")

    # Just test against the first 2 real upcoming events to keep this cheap
    results = []
    for event in events[:2]:
        print(f"\n=== {event['away_team']} @ {event['home_team']} ({event['commence_time']}) ===")
        url = (f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event['id']}/odds"
               f"?apiKey={API_KEY}&regions=us&markets={MARKETS}&oddsFormat=american")
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
                headers_seen = dict(resp.headers)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        print(f"  quota used={headers_seen.get('x-requests-used')} remaining={headers_seen.get('x-requests-remaining')} last_cost={headers_seen.get('x-requests-last')}")
        print(f"  {len(data.get('bookmakers', []))} bookmakers responded")

        # Collect: (market, player, line) -> {book: {over_odds, under_odds}}
        props = {}
        for bm in data.get("bookmakers", []):
            book = bm["key"]
            for mk in bm.get("markets", []):
                market_key = mk["key"]
                # group outcomes by (player description, point)
                by_player = {}
                for oc in mk.get("outcomes", []):
                    player = oc.get("description")
                    point = oc.get("point")
                    key = (market_key, player, point)
                    by_player.setdefault(key, {})[oc["name"]] = oc["price"]
                for key, sides in by_player.items():
                    if "Over" in sides and "Under" in sides:
                        props.setdefault(key, {})[book] = sides

        # Devig + consensus per prop
        edges = []
        for (market_key, player, point), books in props.items():
            if len(books) < 2:
                continue  # need at least 2 books to form a real consensus
            devigged = {}
            for book, sides in books.items():
                p_over_raw = american_to_prob(sides["Over"])
                p_under_raw = american_to_prob(sides["Under"])
                total = p_over_raw + p_under_raw
                devigged[book] = {"over": p_over_raw / total, "under": p_under_raw / total,
                                   "over_odds": sides["Over"], "under_odds": sides["Under"]}
            consensus_over = sum(d["over"] for d in devigged.values()) / len(devigged)
            for book, d in devigged.items():
                edge_over = consensus_over - d["over"]  # positive = this book underprices Over = bet Over here
                edge_under = (1 - consensus_over) - d["under"]
                if edge_over > 0.03:
                    edges.append((edge_over, market_key, player, point, "Over", book, d["over_odds"], consensus_over, d["over"]))
                if edge_under > 0.03:
                    edges.append((edge_under, market_key, player, point, "Under", book, d["under_odds"], 1-consensus_over, d["under"]))

        edges.sort(reverse=True)
        print(f"  {len(props)} real props with 2+ books, {len(edges)} flagged edges > 3pp")
        for edge, market_key, player, point, side, book, odds, consensus, book_prob in edges[:8]:
            print(f"    {edge*100:+.1f}pp  {player} {market_key} {side} {point} @ {book} ({odds})  consensus={consensus*100:.1f}%  book_implied={book_prob*100:.1f}%")


if __name__ == "__main__":
    _debug_prop_consensus()  # TEMPORARY -- real call (capture(SEASON, WEEK)) restored after this check

