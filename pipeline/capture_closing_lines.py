"""
TEMPORARY, one-time script -- real retroactive backtest of the prop devig/consensus engine
(build_prop_value_report in weekly_update.py) against real weeks 1-4 of the 2026 season,
which have already been played. Two honest checks, not one:

  1. CALIBRATION: across EVERY real player prop with 2+ books quoting it (flagged or not),
     bucket the devigged consensus probability and compare to the real hit rate in that
     bucket. This tests the engine's actual load-bearing assumption -- that averaging
     several books' devigged prices gives something close to a true probability -- directly,
     with no betting-strategy question attached yet.

  2. EDGE BACKTEST: among props the LIVE code would have actually flagged (one book >3pp off
     consensus), did the recommended side (the side consensus likes better than that specific
     book's own price) really hit more often than that book's own implied probability, i.e. is
     there real, extractable value past the vig -- or does the "outlier" book turn out to be
     the sharp side as often as the soft side?

Real actual results come from this season's real play-by-play (same source every other real
stat in this app uses), aggregated per real game-week per player -- NOT a second, separately-
trusted data source. Player-name matching between the Odds API's full names and nflverse's
short "F.Surname" PBP convention is best-effort (first initial + last surname token, suffixes
stripped) -- the match rate is reported honestly rather than assumed to be 100%.

player_anytime_td is excluded from the pull: its real outcomes are named "Yes"/"No" by the
Odds API, not "Over"/"Under", so the live devig code's `"Over" in sides and "Under" in sides`
check structurally can never match it -- that market contributes zero live edges today
regardless of true demand, and pulling it here would just spend quota to prove that again.

Quota safety: aborts early (prints why) if real remaining quota drops under 1500, so a mid-
run surprise can't silently drain the whole plan.
"""
import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from weekly_update import SEASON, API_KEY, REV_MAP, fetch_games_csv, fetch_pbp, _american_to_prob

BACKTEST_MARKETS = "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_pass_tds,player_pass_completions,player_rush_attempts"
MIN_EDGE_PP = 0.03
QUOTA_FLOOR = 1500
MARKET_STAT_FIELD = {
    "player_pass_yds": "pass_yards", "player_rush_yds": "rush_yards",
    "player_reception_yds": "rec_yards", "player_receptions": "receptions",
    "player_pass_tds": "pass_tds", "player_pass_completions": "completions",
    "player_rush_attempts": "rush_att",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
def odds_name_to_pbp_short(full_name):
    toks = str(full_name).strip().split()
    if not toks:
        return None
    first = toks[0]
    rest = toks[1:]
    while len(rest) > 1 and rest[-1].rstrip(".").lower() in _SUFFIXES:
        rest.pop()
    surname = rest[-1] if rest else toks[0]
    return f"{first[0]}.{surname}"


def _http_json(url):
    with urllib.request.urlopen(url, timeout=25) as resp:
        return json.loads(resp.read()), dict(resp.headers)


def build_real_week_actuals(season, weeks):
    """Real per-(week, posteam-pair-restricted-by-caller, player short name) actual stat
    lines, aggregated straight from this season's real play-by-play -- same source every
    other real stat in this app already uses, not a second trusted source."""
    path = fetch_pbp(season)
    if path is None:
        raise RuntimeError(f"nflverse hasn't published {season} play-by-play yet -- can't grade anything real.")
    cols = ["week", "season_type", "posteam", "passer", "rusher", "receiver", "play_type",
            "complete_pass", "passing_yards", "rushing_yards", "receiving_yards",
            "pass_touchdown", "rush_touchdown"]
    p = pd.read_parquet(path, columns=cols)
    p = p[(p.season_type == "REG") & (p.week.isin(weeks))]

    actuals = defaultdict(lambda: defaultdict(float))  # (week, short_name) -> field -> value
    team_of = {}  # (week, short_name) -> posteam

    pass_p = p[(p.play_type == "pass") & p.passer.notna()]
    for (wk, name), g in pass_p.groupby(["week", "passer"]):
        key = (wk, name)
        actuals[key]["completions"] += float(g.complete_pass.sum())
        actuals[key]["pass_yards"] += float(g.passing_yards.fillna(0).sum())
        actuals[key]["pass_tds"] += float(g.pass_touchdown.fillna(0).sum())
        team_of[key] = g.posteam.mode().iat[0] if not g.posteam.mode().empty else None

    run_p = p[(p.play_type == "run") & p.rusher.notna()]
    for (wk, name), g in run_p.groupby(["week", "rusher"]):
        key = (wk, name)
        actuals[key]["rush_att"] += float(len(g))
        actuals[key]["rush_yards"] += float(g.rushing_yards.fillna(0).sum())
        actuals[key]["rush_tds"] += float(g.rush_touchdown.fillna(0).sum())
        team_of[key] = team_of.get(key) or (g.posteam.mode().iat[0] if not g.posteam.mode().empty else None)

    rec_p = p[(p.play_type == "pass") & (p.complete_pass == 1) & p.receiver.notna()]
    for (wk, name), g in rec_p.groupby(["week", "receiver"]):
        key = (wk, name)
        actuals[key]["receptions"] += float(len(g))
        actuals[key]["rec_yards"] += float(g.receiving_yards.fillna(0).sum())
        actuals[key]["rec_tds"] += float(g.pass_touchdown.fillna(0).sum())
        team_of[key] = team_of.get(key) or (g.posteam.mode().iat[0] if not g.posteam.mode().empty else None)

    return actuals, team_of


def run():
    games = pd.read_csv(fetch_games_csv())
    weeks = [1, 2, 3, 4]
    wk_games = games[(games.season.astype(str) == str(SEASON)) & (games.week.isin(weeks)) & (games.game_type == "REG")].copy()
    print(f"Real games, weeks {weeks}, season {SEASON}: {len(wk_games)}")

    print("Building real actuals from this season's real play-by-play...")
    actuals, team_of = build_real_week_actuals(SEASON, weeks)
    print(f"  {len(actuals)} real (week, player) stat lines built from real PBP")

    wk_games["kickoff_et_str"] = wk_games["gameday"].astype(str) + " " + wk_games["gametime"].astype(str)
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")

    def kickoff_utc(r):
        dt = datetime.strptime(f"{r.gameday} {r.gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)
        return dt.astimezone(timezone.utc)

    wk_games["kickoff"] = wk_games.apply(kickoff_utc, axis=1)
    wk_games["snapshot"] = wk_games["kickoff"] - timedelta(minutes=30)

    # Group games sharing an identical snapshot timestamp to minimize "historical events" calls.
    by_snapshot = defaultdict(list)
    for _, r in wk_games.iterrows():
        by_snapshot[r["snapshot"].strftime("%Y-%m-%dT%H:%M:%SZ")].append(r)

    print(f"{len(by_snapshot)} distinct snapshot times across {len(wk_games)} games")

    all_props = []       # every real prop with 2+ books: full calibration pool
    all_edges = []       # subset flagged by the live >3pp rule
    events_calls = 0
    odds_calls = 0
    last_remaining = None
    skipped_quota = False

    for snap, rows in sorted(by_snapshot.items()):
        if skipped_quota:
            break
        events_url = f"https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/events?apiKey={API_KEY}&date={snap}"
        try:
            payload, hdrs = _http_json(events_url)
        except Exception as e:
            print(f"  [{snap}] events call failed: {e}")
            continue
        events_calls += 1
        last_remaining = int(hdrs.get("x-requests-remaining", 0) or 0)
        events = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            print(f"  [{snap}] unexpected events shape, skipping this snapshot")
            continue

        for r in rows:
            if last_remaining is not None and last_remaining < QUOTA_FLOOR:
                print(f"STOPPING: remaining quota {last_remaining} under safety floor {QUOTA_FLOOR}.")
                skipped_quota = True
                break
            home_full, away_full = REV_MAP[r.home_team], REV_MAP[r.away_team]
            gid = f"{r.away_team.lower()}-{r.home_team.lower()}"
            match = next((e for e in events if e.get("home_team") == home_full and e.get("away_team") == away_full), None)
            if not match:
                print(f"  wk{r.week} {gid}: not found in historical events at {snap} -- skipped")
                continue
            odds_url = (f"https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/events/{match['id']}/odds"
                        f"?apiKey={API_KEY}&regions=us&markets={BACKTEST_MARKETS}&oddsFormat=american&date={snap}")
            try:
                odds_payload, hdrs2 = _http_json(odds_url)
            except Exception as e:
                print(f"  wk{r.week} {gid}: odds call failed: {e}")
                continue
            odds_calls += 1
            last_remaining = int(hdrs2.get("x-requests-remaining", 0) or 0)
            time.sleep(0.2)

            event_odds = odds_payload.get("data", odds_payload) if isinstance(odds_payload, dict) else odds_payload
            bookmakers = event_odds.get("bookmakers", []) if isinstance(event_odds, dict) else []
            if not bookmakers:
                continue

            props = {}
            for bm in bookmakers:
                book = bm["key"]
                for mk in bm.get("markets", []):
                    market_key = mk["key"]
                    by_player = {}
                    for oc in mk.get("outcomes", []):
                        player, point = oc.get("description"), oc.get("point")
                        by_player.setdefault((market_key, player, point), {})[oc["name"]] = oc["price"]
                    for key, sides in by_player.items():
                        if "Over" in sides and "Under" in sides:
                            props.setdefault(key, {})[book] = sides

            for (market_key, player, point), books in props.items():
                if len(books) < 2:
                    continue
                devigged = {}
                for book, sides in books.items():
                    p_over = _american_to_prob(sides["Over"])
                    p_under = _american_to_prob(sides["Under"])
                    total = p_over + p_under
                    devigged[book] = {"over": p_over / total, "under": p_under / total,
                                       "over_odds": sides["Over"], "under_odds": sides["Under"]}
                consensus_over = sum(d["over"] for d in devigged.values()) / len(devigged)

                short = odds_name_to_pbp_short(player)
                stat_field = MARKET_STAT_FIELD.get(market_key)
                actual_key = (int(r.week), short)
                actual_val = actuals.get(actual_key, {}).get(stat_field) if stat_field else None
                player_team = team_of.get(actual_key)
                team_ok = player_team in (r.home_team, r.away_team) if player_team else False
                matched = actual_val is not None and team_ok

                for book, d in devigged.items():
                    all_props.append({"consensus": consensus_over, "book_p": d["over"], "matched": matched,
                                       "actual": actual_val, "line": point, "side": "Over"})
                    all_props.append({"consensus": 1 - consensus_over, "book_p": d["under"], "matched": matched,
                                       "actual": actual_val, "line": point, "side": "Under"})

                    edge_over = consensus_over - d["over"]
                    edge_under = (1 - consensus_over) - d["under"]
                    if edge_over > MIN_EDGE_PP:
                        all_edges.append({"week": int(r.week), "game_id": gid, "market": market_key, "player": player,
                                           "short": short, "line": point, "side": "Over", "book": book,
                                           "odds": d["over_odds"], "edge_pp": edge_over * 100,
                                           "consensus_pct": consensus_over * 100, "book_implied_pct": d["over"] * 100,
                                           "n_books": len(books), "actual": actual_val, "matched": matched})
                    if edge_under > MIN_EDGE_PP:
                        all_edges.append({"week": int(r.week), "game_id": gid, "market": market_key, "player": player,
                                           "short": short, "line": point, "side": "Under", "book": book,
                                           "odds": d["under_odds"], "edge_pp": edge_under * 100,
                                           "consensus_pct": (1 - consensus_over) * 100, "book_implied_pct": d["under"] * 100,
                                           "n_books": len(books), "actual": actual_val, "matched": matched})
            print(f"  wk{r.week} {gid}: {len(props)} real props (2+ books), remaining quota={last_remaining}")

    print(f"\n=== calls made: {events_calls} events + {odds_calls} event-odds, final remaining quota={last_remaining} ===\n")

    # ---- 1. Calibration: real hit rate vs. real devigged probability, matched props only ----
    print("=== CALIBRATION (all real props with 2+ books, flagged or not) ===")
    buckets = defaultdict(lambda: [0, 0])  # bucket -> [hits, total]
    for pr in all_props:
        if not pr["matched"] or pr["actual"] is None or pr["line"] is None:
            continue
        if pr["actual"] == pr["line"]:
            continue  # real push, excluded same as a sportsbook would
        hit = (pr["actual"] > pr["line"]) if pr["side"] == "Over" else (pr["actual"] < pr["line"])
        b = int(pr["consensus"] * 10) * 10  # 0,10,...,90
        buckets[b][0] += int(hit)
        buckets[b][1] += 1
    for b in sorted(buckets):
        hits, total = buckets[b]
        print(f"  consensus {b}-{b+10}%: real hit rate {hits}/{total} = {hits/total*100:.1f}%" if total else f"  consensus {b}-{b+10}%: no graded props")

    # ---- 2. Edge backtest: the live >3pp flagged picks only ----
    print(f"\n=== EDGE BACKTEST ({len(all_edges)} real flagged edges, >{MIN_EDGE_PP*100:.0f}pp) ===")
    graded = [e for e in all_edges if e["matched"] and e["actual"] is not None and e["actual"] != e["line"]]
    print(f"  matched to a real graded result: {len(graded)} / {len(all_edges)} ({len(graded)/len(all_edges)*100:.0f}% match rate)" if all_edges else "  no edges flagged at all")
    wins = 0
    breakevens = []
    for e in graded:
        hit = (e["actual"] > e["line"]) if e["side"] == "Over" else (e["actual"] < e["line"])
        wins += int(hit)
        book_p = e["book_implied_pct"] / 100.0
        breakevens.append(book_p)
    n = len(graded)
    if n:
        win_rate = wins / n
        avg_breakeven = sum(breakevens) / n
        print(f"  record: {wins}-{n-wins} ({win_rate*100:.1f}%)")
        print(f"  avg. book-implied breakeven on these same bets (the standard they need to clear): {avg_breakeven*100:.1f}%")
        import math
        p = avg_breakeven
        pval = sum(math.comb(n, k) * (p ** k) * ((1 - p) ** (n - k)) for k in range(wins, n + 1))
        print(f"  one-sided exact binomial p-value (beats breakeven, no scipy needed): {pval:.4f}")
    else:
        print("  no graded edges -- can't compute a real win rate.")

    print("\n=== sample of individual graded edges ===")
    for e in sorted(graded, key=lambda x: -x["edge_pp"])[:20]:
        hit = (e["actual"] > e["line"]) if e["side"] == "Over" else (e["actual"] < e["line"])
        print(f"  wk{e['week']} {e['player']} {e['market']} {e['side']} {e['line']} @ {e['book']} ({e['odds']})"
              f"  edge={e['edge_pp']:.1f}pp  real actual={e['actual']}  -> {'WIN' if hit else 'LOSS'}")


if __name__ == "__main__":
    run()
