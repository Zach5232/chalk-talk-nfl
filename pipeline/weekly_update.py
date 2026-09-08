"""
Chalk Talk weekly update pipeline.

Run this once a week (Tuesday) during the season. It does four things:
  1. Refreshes team-game EPA splits through the most recently completed week (walk-forward, no lookahead)
  2. Fits the ridge power rating model for the CURRENT week's projections
  3. Pulls this week's odds (best-available price per book, per game) from The Odds API
  4. Fills in closing lines + final results for the PREVIOUS week, once it's actually final

Output: prints ready-to-paste JS for the RATINGS, GAMES, BOOKS, and CLOSING_RESULTS
arrays/objects in ChalkTalk.html.

--- CONFIG: edit these each week ---
"""
import subprocess, json, csv, os
import pandas as pd
import numpy as np

SEASON = 2026          # real season, kicks off 2026-09-09
WEEK = 1               # Week 1 -- no 2026 games played yet, ratings run purely off the
                        # carryover-from-2025 prior (see run_ratings: hist is empty for
                        # week 1, so off/deft = prior_off/prior_def directly)

# API_KEY now comes from config_local.py (gitignored -- never committed) or the ODDS_API_KEY
# env var, NOT hardcoded here. This file is going into a GitHub repo, and a real key sitting
# in source is the kind of thing that's easy to forget is there once it's in git history --
# see config_local.example.py for the format. Falls back to the env var so this also works
# in CI/cloud runs where there's no local file at all.
try:
    from config_local import API_KEY
except ImportError:
    API_KEY = os.environ.get("ODDS_API_KEY", "")
    if not API_KEY:
        raise RuntimeError(
            "No API key found. Copy config_local.example.py to config_local.py and fill in "
            "your real Odds API key, or set the ODDS_API_KEY environment variable."
        )
MODE = "live"          # real season is live -- pulls the current real odds board
HIST_DATE = "2025-11-04T12:00:00Z"  # unused in live mode, left for reference/backtesting

TEAM_MAP = {
    "Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL","Buffalo Bills":"BUF",
    "Carolina Panthers":"CAR","Chicago Bears":"CHI","Cincinnati Bengals":"CIN","Cleveland Browns":"CLE",
    "Dallas Cowboys":"DAL","Denver Broncos":"DEN","Detroit Lions":"DET","Green Bay Packers":"GB",
    "Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX","Kansas City Chiefs":"KC",
    "Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC","Los Angeles Rams":"LA","Miami Dolphins":"MIA",
    "Minnesota Vikings":"MIN","New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG",
    "New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT","San Francisco 49ers":"SF",
    "Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB","Tennessee Titans":"TEN","Washington Commanders":"WAS",
}
REV_MAP = {v: k for k, v in TEAM_MAP.items()}


# ---------- STEP 1: play-by-play -> team-game EPA splits ----------
def fetch_pbp(season):
    """
    Returns the local path to that season's pbp parquet, or None if nflverse hasn't
    published it yet -- true for the CURRENT season before any games have been played
    (e.g. the day before Week 1 kicks off). curl with just -sL still exits 0 on a 404 and
    writes the error page to the file, so this checks the real HTTP status instead of
    trusting the exit code, and never silently hands back a bad file.
    """
    path = f"/home/claude/pipeline/pbp_{season}.parquet"
    url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
    code = subprocess.run(["curl", "-sL", "-o", path, "-w", "%{http_code}", url],
                           capture_output=True, text=True).stdout.strip()
    if code != "200":
        return None
    return path

# Columns build_team_games/build_havoc_games/build_st_games produce -- reused to hand back
# a correctly-shaped EMPTY frame when there's no pbp file yet, instead of crashing.
_TEAM_GAMES_COLS = ["game_id","week","team","opp","off_epa","plays","hfa","def_epa_allowed","season"]
_VALUE_GAMES_COLS = ["week","team","opp","hfa","value"]

def build_team_games(path, season):
    if path is None:
        return pd.DataFrame(columns=_TEAM_GAMES_COLS)
    cols = ["game_id","season","week","posteam","defteam","epa","play_type","season_type","home_team","away_team"]
    pbp = pd.read_parquet(path, columns=cols)
    pbp = pbp[pbp.season_type == "REG"]
    pbp = pbp[pbp.play_type.isin(["pass","run"])]
    pbp = pbp[pbp.epa.notna() & pbp.posteam.notna() & pbp.defteam.notna()]

    rows = []
    for (gid, wk, post, deft), grp in pbp.groupby(["game_id","week","posteam","defteam"]):
        rows.append({"game_id": gid, "week": wk, "team": post, "opp": deft,
                      "off_epa": grp.epa.mean(), "plays": len(grp)})
    tg = pd.DataFrame(rows)
    game_info = pbp.drop_duplicates("game_id")[["game_id","home_team","away_team"]]
    tg = tg.merge(game_info, on="game_id", how="left")
    tg["home"] = tg["team"] == tg["home_team"]

    # Neutral-site games (international games, occasional relocated games) get zero home-field
    # advantage in the fit, rather than crediting/penalizing the "designated" home team as if
    # they had a real home-field edge. Source: games.csv's own location field.
    neutral_ids = get_neutral_game_ids()
    tg["hfa"] = tg.apply(lambda r: 0.0 if r.game_id in neutral_ids else (1.0 if r.home else -1.0), axis=1)

    tg = tg.drop(columns=["home_team","away_team"])
    def_map = tg.set_index(["game_id","team"])["off_epa"]
    tg["def_epa_allowed"] = tg.apply(lambda r: def_map.get((r.game_id, r.opp), np.nan), axis=1)
    tg["season"] = season
    return tg


_NEUTRAL_IDS_CACHE = None
def get_neutral_game_ids():
    global _NEUTRAL_IDS_CACHE
    if _NEUTRAL_IDS_CACHE is None:
        g = pd.read_csv("/home/claude/odds_pull/games.csv")
        _NEUTRAL_IDS_CACHE = set(g[g.location == "Neutral"]["game_id"])
    return _NEUTRAL_IDS_CACHE


# ---------- STEP 2: ridge power rating fit (walk-forward, no lookahead) ----------
def fit_split(hist, teams, tix, n, lam=3.0, halflife=6.0, prior_off=None, prior_def=None, value_col="off_epa"):
    maxwk = hist.week.max()
    w = 0.5 ** ((maxwk - hist.week) / halflife)
    rows, y = [], []
    for _, r in hist.iterrows():
        x = np.zeros(2*n + 1)
        x[tix[r.team]] += 1
        x[n + tix[r.opp]] -= 1
        x[2*n] = r.hfa
        rows.append(x); y.append(getattr(r, value_col))
    X = np.array(rows); y = np.array(y); W = np.diag(w.values)
    target = np.zeros(2*n + 1)
    if prior_off is not None:
        for t in teams: target[tix[t]] = prior_off.get(t, 0.0)
    if prior_def is not None:
        for t in teams: target[n + tix[t]] = prior_def.get(t, 0.0)
    A = X.T @ W @ X + lam*np.eye(2*n+1)
    A[2*n, 2*n] -= lam
    b = X.T @ W @ y + lam*target
    b[2*n] -= lam*target[2*n]
    beta = np.linalg.solve(A, b)
    off = pd.Series(beta[:n], index=teams)
    deft = pd.Series(beta[n:2*n], index=teams)
    return off, deft, beta[2*n]


# ---------- Havoc rate: opponent-adjusted defensive disruption ----------
# havoc = sack, TFL, forced fumble, INT, or pass breakup on that play (validated methodology
# from the earlier build session -- "team" here is the DEFENSE creating havoc, "opp" is the
# offense facing it; only the team-side coefficient is meaningful, the opp-side is discarded).
def build_havoc_games(pbp_path):
    if pbp_path is None:
        return pd.DataFrame(columns=_VALUE_GAMES_COLS)
    cols = ["game_id","week","posteam","defteam","play_type","season_type","sack","tackled_for_loss",
            "fumble_forced","interception","pass_defense_1_player_id","home_team","away_team"]
    p = pd.read_parquet(pbp_path, columns=cols)
    p = p[(p.season_type=="REG") & p.play_type.isin(["pass","run"])]
    p["havoc"] = ((p.sack==1)|(p.tackled_for_loss==1)|(p.fumble_forced==1)|
                  (p.interception==1)|(p.pass_defense_1_player_id.notna())).astype(int)
    dg = p.groupby(["game_id","week","defteam","posteam"]).havoc.agg(["sum","count"]).reset_index()
    dg.columns = ["game_id","week","team","opp","havoc_plays","def_snaps"]
    dg["value"] = dg.havoc_plays / dg.def_snaps
    game_info = p.drop_duplicates(["game_id","defteam","posteam"])[["game_id","defteam","posteam","home_team"]]
    game_info.columns = ["game_id","team","opp","home_team"]
    dg = dg.merge(game_info, on=["game_id","team","opp"], how="left")
    dg["home"] = dg["team"] == dg["home_team"]
    neutral_ids = get_neutral_game_ids()
    dg["hfa"] = dg.apply(lambda r: 0.0 if r.game_id in neutral_ids else (1.0 if r.home else -1.0), axis=1)
    return dg[["week","team","opp","hfa","value"]]


# ---------- Special teams EPA: opponent-adjusted, same 2-sided design as core model ----------
def build_st_games(pbp_path):
    if pbp_path is None:
        return pd.DataFrame(columns=_VALUE_GAMES_COLS)
    cols = ["game_id","week","posteam","defteam","play_type","epa","season_type","home_team"]
    p = pd.read_parquet(pbp_path, columns=cols)
    p = p[(p.season_type=="REG") & p.play_type.isin(["field_goal","punt","kickoff","extra_point"])]
    p = p[p.epa.notna()]
    st = p.groupby(["game_id","week","posteam","defteam"]).agg(value=("epa","mean"), home_team=("home_team","first")).reset_index()
    st["home"] = st["posteam"] == st["home_team"]
    neutral_ids = get_neutral_game_ids()
    st["hfa"] = st.apply(lambda r: 0.0 if r.game_id in neutral_ids else (1.0 if r.home else -1.0), axis=1)
    return st.rename(columns={"posteam":"team","defteam":"opp"})[["week","team","opp","hfa","value"]]


def _real_teams_for_season(season):
    """32 real team abbreviations for this season, from the schedule itself -- doesn't
    depend on any current-season pbp existing yet (true for Week 1 before kickoff)."""
    games = pd.read_csv("/home/claude/odds_pull/games.csv")
    g = games[games.season.astype(str) == str(season)]
    return sorted(set(g.home_team) | set(g.away_team))

def run_ratings(season, week, prior_season_pbp_path):
    pbp_path = fetch_pbp(season)
    tg = build_team_games(pbp_path, season)
    teams = sorted(tg.team.unique()) if len(tg) else _real_teams_for_season(season)
    n = len(teams); tix = {t:i for i,t in enumerate(teams)}

    prior_tg = build_team_games(prior_season_pbp_path, season-1)
    off_prior_final, def_prior_final, hfa_prior = fit_split(prior_tg, teams, tix, n, lam=3.0, halflife=6.0)
    prior_off = (off_prior_final * 0.51).to_dict()
    prior_def = (def_prior_final * 0.05).to_dict()

    # fit pts_per_epa using ALL completed games so far this season
    games = pd.read_csv("/home/claude/odds_pull/games.csv")
    g_season = games[(games.season.astype(str)==str(season)) & (games.game_type=="REG")].copy()
    completed = g_season[g_season.result.notna() & (g_season.week < week)]
    tg_idx = tg.set_index(["game_id","team"])
    epa_diffs, margins = [], []
    for _, r in completed.iterrows():
        try:
            ho = tg_idx.loc[(r.game_id, r.home_team), "off_epa"]
            ao = tg_idx.loc[(r.game_id, r.away_team), "off_epa"]
        except KeyError:
            continue
        epa_diffs.append(ho - ao); margins.append(float(r.result))
    if len(epa_diffs) >= 5:
        pts_per_epa = np.linalg.lstsq(np.column_stack([epa_diffs, np.ones(len(epa_diffs))]), margins, rcond=None)[0][0]
    else:
        pts_per_epa = 44.0  # fallback for very early season, before enough games exist

    # walk-forward rating as of THIS week (only games from weeks < week)
    hist = tg[tg.week < week]
    if len(hist) == 0:
        off, deft, hfa = pd.Series(prior_off), pd.Series(prior_def), hfa_prior
    else:
        off, deft, hfa = fit_split(hist, teams, tix, n, lam=3.0, halflife=6.0, prior_off=prior_off, prior_def=prior_def)

    # also compute LAST week's ratings, for the "move" column
    hist_prev = tg[tg.week < week - 1]
    if len(hist_prev) == 0:
        off_prev, deft_prev = pd.Series(prior_off), pd.Series(prior_def)
    else:
        off_prev, deft_prev, _ = fit_split(hist_prev, teams, tix, n, lam=3.0, halflife=6.0, prior_off=prior_off, prior_def=prior_def)

    # ---- havoc rate: opponent-adjusted defensive disruption, walk-forward ----
    havoc_games = build_havoc_games(pbp_path)
    havoc_hist = havoc_games[havoc_games.week < week]
    if len(havoc_hist) == 0:
        havoc_rating = pd.Series(0.0, index=teams)
    else:
        havoc_rating, _, _ = fit_split(havoc_hist, teams, tix, n, lam=3.0, halflife=6.0, value_col="value")

    # ---- special teams EPA: opponent-adjusted, walk-forward ----
    st_games = build_st_games(pbp_path)
    st_hist = st_games[st_games.week < week]
    if len(st_hist) == 0:
        st_rating = pd.Series(0.0, index=teams)
    else:
        st_rating, _, _ = fit_split(st_hist, teams, tix, n, lam=3.0, halflife=6.0, value_col="value")

    return {
        "teams": teams, "off": off, "deft": deft, "hfa": hfa, "pts_per_epa": pts_per_epa,
        "off_prev": off_prev, "deft_prev": deft_prev,
        "havoc_rating": havoc_rating, "st_rating": st_rating,
        "games_this_week": g_season[g_season.week == week],
        "games_prev_week": g_season[g_season.week == week - 1],
    }


# ---------- STEP 3: odds pull (this week's games, per-book) ----------
def pull_week_odds(mode, api_key, hist_date=None):
    if mode == "live":
        url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/?apiKey={api_key}&regions=us&markets=spreads&oddsFormat=american"
    else:
        url = f"https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/odds/?apiKey={api_key}&regions=us&markets=spreads&oddsFormat=american&date={hist_date}"
    out_path = "/home/claude/pipeline/week_odds.json"
    subprocess.run(["curl","-s","-o",out_path,url], check=True)
    d = json.load(open(out_path))
    return d.get("data", d) if isinstance(d, dict) else d


def build_books_for_week(odds_data, week_games):
    books_out = {}
    for _, r in week_games.iterrows():
        home_full = REV_MAP[r.home_team]; away_full = REV_MAP[r.away_team]
        match = next((g for g in odds_data if g["home_team"]==home_full and g["away_team"]==away_full), None)
        if not match: continue
        rows = []
        for bm in match.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk["key"] == "spreads":
                    hp = ap = ho = ao = None
                    for oc in mk["outcomes"]:
                        if oc["name"] == home_full: hp, ho = oc["point"], oc["price"]
                        elif oc["name"] == away_full: ap, ao = oc["point"], oc["price"]
                    if hp is not None:
                        rows.append({"book": bm["key"], "home_pt": hp, "home_odds": ho, "away_odds": ao})
        if rows:
            gid = f"{r.away_team.lower()}-{r.home_team.lower()}"
            books_out[gid] = {"books": rows}
    return books_out


# ---------- STEP 4: previous week's closing lines + results ----------
def build_closing_results(games_prev_week):
    out = {}
    for _, r in games_prev_week.iterrows():
        if pd.isna(r.result) or r.result == "":
            continue
        gid = f"{r.away_team.lower()}-{r.home_team.lower()}"
        out[gid] = {"close_home": float(r.spread_line), "away_score": int(r.away_score), "home_score": int(r.home_score)}
    return out


# ---------- STEP 5: weather (outdoor/retractable stadiums only) ----------
import sys
sys.path.insert(0, "/home/claude/pipeline")
from stadiums import STADIUMS

def pull_weather_for_week(games_this_week):
    """
    Pulls a forecast for each outdoor or retractable-roof stadium hosting a game this week.
    True domes are skipped entirely -- weather can't affect the game there.
    Uses Open-Meteo (free, no API key). Requires api.open-meteo.com on the network allowlist.
    """
    weather = {}
    for _, r in games_this_week.iterrows():
        home = r.home_team
        stad = STADIUMS.get(home)
        if not stad:
            continue
        if stad["roof"] == "dome":
            weather[f"{r.away_team.lower()}-{home.lower()}"] = {"roof": "dome", "note": "Indoors -- weather is not a factor."}
            continue
        game_date = pd.to_datetime(r.gameday).strftime("%Y-%m-%d")
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={stad['lat']}&longitude={stad['lon']}"
               f"&daily=temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_probability_max"
               f"&temperature_unit=fahrenheit&windspeed_unit=mph&timezone=America%2FNew_York"
               f"&start_date={game_date}&end_date={game_date}")
        out_path = f"/home/claude/pipeline/wx_{r.home_team}_{r.week}.json"
        code = subprocess.run(["curl","-s","-o",out_path,"-w","%{http_code}",url], capture_output=True, text=True).stdout.strip()
        gid = f"{r.away_team.lower()}-{home.lower()}"
        if code != "200":
            weather[gid] = {"roof": stad["roof"], "error": f"weather pull failed (HTTP {code}) -- check that api.open-meteo.com is on your network allowlist"}
            continue
        d = json.load(open(out_path))
        daily = d.get("daily", {})
        try:
            weather[gid] = {
                "roof": stad["roof"],
                "temp_high": daily["temperature_2m_max"][0],
                "temp_low": daily["temperature_2m_min"][0],
                "wind_mph": daily["windspeed_10m_max"][0],
                "precip_pct": daily["precipitation_probability_max"][0],
            }
        except (KeyError, IndexError):
            weather[gid] = {"roof": stad["roof"], "error": "forecast not available yet (too far out) -- try again closer to game day"}
    return weather


# ---------- QB-swap adjustment (validated against 178 real 2024-2025 swap events: r=0.463, p<0.0001) ----------
# When the current week's starter isn't the QB whose snaps the season's rating is built on,
# shift the offensive rating using the GAP between the two QBs' own career EPA/play -- not
# the departed starter's number, and not a flat positional penalty. Pass-through is 0.465,
# meaning less than half the raw QB skill gap actually reaches the team-level rating; the
# rest is absorbed by O-line/scheme/weapons, which don't change when the QB does.
QB_SWAP_PASSTHROUGH = 0.465
QB_RELIABLE_SAMPLE_MIN = 50  # attempts needed to trust a QB's own career number

def build_qb_career_ratings(pbp_paths_and_seasons):
    """pbp_paths_and_seasons: list of (path, season) tuples. Returns per-passer career EPA/play and CPOE."""
    frames = []
    for path, season in pbp_paths_and_seasons:
        cols = ["passer","epa","cpoe","play_type","season_type"]
        p = pd.read_parquet(path, columns=cols)
        p = p[(p.season_type=="REG") & (p.play_type=="pass")].dropna(subset=["passer"])
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)
    qb = allp.groupby("passer").agg(career_epa=("epa","mean"), career_cpoe=("cpoe","mean"), career_n=("epa","size")).reset_index()
    return qb.set_index("passer")

def qb_swap_adjustment(current_starter, established_qb, qb_career_ratings):
    """
    Returns (adjustment_in_epa_units, note). adjustment gets ADDED to the team's current
    offensive rating (negative if the new starter grades worse than the established QB).
    Returns 0.0 with a flag if either QB lacks a reliable sample (e.g. a rookie) -- in that
    case this mechanism honestly has nothing to say, and should NOT be papered over with a
    guessed number.
    """
    if current_starter == established_qb:
        return 0.0, "no swap"
    if current_starter not in qb_career_ratings.index or established_qb not in qb_career_ratings.index:
        return 0.0, "insufficient career data for one or both QBs -- adjustment not applied"
    new_n = qb_career_ratings.loc[current_starter, "career_n"]
    est_n = qb_career_ratings.loc[established_qb, "career_n"]
    if new_n < QB_RELIABLE_SAMPLE_MIN or est_n < QB_RELIABLE_SAMPLE_MIN:
        return 0.0, f"sample too small (new={new_n}, established={est_n}) -- adjustment not applied"
    gap = qb_career_ratings.loc[current_starter, "career_epa"] - qb_career_ratings.loc[established_qb, "career_epa"]
    adj = QB_SWAP_PASSTHROUGH * gap
    return adj, f"applied: {current_starter} vs {established_qb} career EPA gap {gap:+.3f} -> {adj:+.3f} adjustment"


# ================= RUN =================
def run_rating_history(season, week, prior_season_pbp_path):
    """
    Computes the walk-forward rating for EVERY week from 1 through the current week
    (not just this week), so the dashboard's team pages can show a real trend line
    instead of a single snapshot. Reuses the exact same fit machinery as run_ratings --
    just loops it.
    """
    pbp_path = fetch_pbp(season)
    tg = build_team_games(pbp_path, season)
    teams = sorted(tg.team.unique()) if len(tg) else _real_teams_for_season(season)
    n = len(teams); tix = {t:i for i,t in enumerate(teams)}

    prior_tg = build_team_games(prior_season_pbp_path, season-1)
    off_prior_final, def_prior_final, _ = fit_split(prior_tg, teams, tix, n, lam=3.0, halflife=6.0)
    prior_off = (off_prior_final * 0.51).to_dict()
    prior_def = (def_prior_final * 0.05).to_dict()

    havoc_games = build_havoc_games(pbp_path)
    st_games = build_st_games(pbp_path)

    games = pd.read_csv("/home/claude/odds_pull/games.csv")
    g_season = games[(games.season.astype(str)==str(season)) & (games.game_type=="REG")].copy()

    history = {t: [] for t in teams}
    for wk in range(1, week + 1):
        completed = g_season[g_season.result.notna() & (g_season.week < wk)]
        tg_idx = tg.set_index(["game_id","team"])
        epa_diffs, margins = [], []
        for _, r in completed.iterrows():
            try:
                ho = tg_idx.loc[(r.game_id, r.home_team), "off_epa"]
                ao = tg_idx.loc[(r.game_id, r.away_team), "off_epa"]
            except KeyError:
                continue
            epa_diffs.append(ho - ao); margins.append(float(r.result))
        pts_per_epa = (np.linalg.lstsq(np.column_stack([epa_diffs, np.ones(len(epa_diffs))]), margins, rcond=None)[0][0]
                       if len(epa_diffs) >= 5 else 44.0)

        hist = tg[tg.week < wk]
        if len(hist) == 0:
            off, deft = pd.Series(prior_off), pd.Series(prior_def)
        else:
            off, deft, _ = fit_split(hist, teams, tix, n, lam=3.0, halflife=6.0, prior_off=prior_off, prior_def=prior_def)

        havoc_hist = havoc_games[havoc_games.week < wk]
        havoc_rating = (fit_split(havoc_hist, teams, tix, n, lam=3.0, halflife=6.0, value_col="value")[0]
                        if len(havoc_hist) > 0 else pd.Series(0.0, index=teams))
        st_hist = st_games[st_games.week < wk]
        st_rating = (fit_split(st_hist, teams, tix, n, lam=3.0, halflife=6.0, value_col="value")[0]
                     if len(st_hist) > 0 else pd.Series(0.0, index=teams))

        for t in teams:
            off_pts = round(off[t]*pts_per_epa, 1); def_pts = round(deft[t]*pts_per_epa, 1)
            history[t].append({
                "week": wk, "overall_pts": round(off_pts-def_pts, 1),
                "off_pts": off_pts, "def_pts": def_pts,
                "st_pts": round(st_rating[t]*pts_per_epa, 1),
                "havoc_pts": round(havoc_rating[t]*100, 1),
            })
    return history


if __name__ == "__main__":
    print(f"=== Chalk Talk weekly update: season {SEASON}, week {WEEK} ({MODE} mode) ===\n")

    ratings = run_ratings(SEASON, WEEK, prior_season_pbp_path="/home/claude/odds_pull/pbp_2024.parquet"
                           if SEASON == 2025 else fetch_pbp(SEASON - 1))

    odds_data = pull_week_odds(MODE, API_KEY, HIST_DATE)
    books = build_books_for_week(odds_data, ratings["games_this_week"])
    closing = build_closing_results(ratings["games_prev_week"])
    weather = pull_weather_for_week(ratings["games_this_week"])

    # ---- RATINGS array ----
    rows = []
    for t in ratings["teams"]:
        off_pts = round(ratings["off"][t] * ratings["pts_per_epa"], 1)
        def_pts = round(ratings["deft"][t] * ratings["pts_per_epa"], 1)
        overall = round(off_pts - def_pts, 1)
        off_prev_pts = ratings["off_prev"][t] * ratings["pts_per_epa"]
        def_prev_pts = ratings["deft_prev"][t] * ratings["pts_per_epa"]
        overall_prev = off_prev_pts - def_prev_pts
        st_pts = round(ratings["st_rating"][t] * ratings["pts_per_epa"], 1)          # same units as off/def: pts
        havoc_pts = round(ratings["havoc_rating"][t] * 100, 1)                        # percentage-point deviation from average havoc rate, NOT the points scale
        rows.append({"team": t, "off_pts": off_pts, "def_pts": def_pts, "overall_pts": overall,
                      "st_pts": st_pts, "havoc_pts": havoc_pts,
                      "_overall_prev": overall_prev})
    rows_sorted_now = sorted(rows, key=lambda r: -r["overall_pts"])
    rows_sorted_prev = sorted(rows, key=lambda r: -r["_overall_prev"])
    prev_rank = {r["team"]: i for i, r in enumerate(rows_sorted_prev)}
    for i, r in enumerate(rows_sorted_now):
        r["move"] = prev_rank[r["team"]] - i
        del r["_overall_prev"]

    print("--- RATINGS (paste into const RATINGS = [ ... ]) ---")
    print(json.dumps(rows_sorted_now, indent=1))

    print(f"\npts_per_epa used: {round(ratings['pts_per_epa'],2)}")
    print(f"home field advantage (epa): {round(ratings['hfa'],4)}")

    # ---- GAMES array (this week, model line vs market consensus) ----
    games_out = []
    for _, r in ratings["games_this_week"].iterrows():
        h, a = r.home_team, r.away_team
        if h not in ratings["off"].index or a not in ratings["off"].index:
            continue
        home_net = ratings["off"][h] - ratings["deft"][a]
        away_net = ratings["off"][a] - ratings["deft"][h]
        model_home_favored = (home_net - away_net + ratings["hfa"]) * ratings["pts_per_epa"]
        gid = f"{a.lower()}-{h.lower()}"
        book_entry = books.get(gid)
        market_home_favored = -float(book_entry["books"][0]["home_pt"]) if book_entry else None
        games_out.append({
            "id": gid, "away": a, "home": h,
            "model": round(-model_home_favored, 2),
            "market": round(-market_home_favored, 2) if market_home_favored is not None else None,
            "ah": None, "aa": None,
            "blurb": "(auto-generated placeholder -- write-up not yet produced by this pipeline)"
        })
    print("\n--- GAMES (paste into const GAMES = [ ... ], write-ups still need a pass) ---")
    print(json.dumps(games_out, indent=1))

    print("\n--- BOOKS (paste into const BOOKS = { ... }) ---")
    print(json.dumps(books, indent=1)[:3000], "\n...(truncated for display)" if len(json.dumps(books))>3000 else "")

    print("\n--- CLOSING_RESULTS additions (merge into const CLOSING_RESULTS = { ... }) ---")
    print(json.dumps(closing, indent=1))

    print("\n--- WEATHER (paste into const WEATHER = { ... }) ---")
    print(json.dumps(weather, indent=1))

    print("\n--- RATING_HISTORY (paste into const RATING_HISTORY = { ... } -- merge/replace week-by-week) ---")
    history = run_rating_history(SEASON, WEEK, prior_season_pbp_path="/home/claude/odds_pull/pbp_2024.parquet"
                                  if SEASON == 2025 else fetch_pbp(SEASON - 1))
    print(json.dumps(history, indent=1)[:2000], "\n...(truncated for display, full output is per-team, per-week)")


# ---------- Player-level metrics: QB CPOE trend, WR/TE YAC-over-expected, RB rushing EPA ----------
def build_qb_weekly_history(pbp_paths_and_seasons):
    """Per-passer, per-week CPOE and EPA/dropback -- powers the QB trend chart."""
    frames = []
    for path, season in pbp_paths_and_seasons:
        cols = ["passer","week","epa","cpoe","play_type","season_type"]
        p = pd.read_parquet(path, columns=cols)
        p = p[(p.season_type=="REG") & (p.play_type=="pass")].dropna(subset=["passer"])
        p["season"] = season
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)
    wk = allp.groupby(["passer","season","week"]).agg(
        cpoe=("cpoe","mean"), epa_per_dropback=("epa","mean"), attempts=("epa","size")
    ).reset_index()
    return wk

def build_qb_leaderboard(pbp_paths_and_seasons, min_attempts=100):
    """Season-long QB leaderboard, sorted by CPOE."""
    frames = []
    for path, season in pbp_paths_and_seasons:
        cols = ["passer","epa","cpoe","play_type","season_type"]
        p = pd.read_parquet(path, columns=cols)
        p = p[(p.season_type=="REG") & (p.play_type=="pass")].dropna(subset=["passer"])
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)
    lb = allp.groupby("passer").agg(cpoe=("cpoe","mean"), epa_per_dropback=("epa","mean"), attempts=("epa","size")).reset_index()
    lb = lb[lb.attempts >= min_attempts].sort_values("cpoe", ascending=False)
    return lb

def build_receiver_yac_oe(pbp_paths_and_seasons, min_targets=20):
    """Season-long receiver leaderboard: actual YAC minus nflverse's own expected-YAC model."""
    frames = []
    for path, season in pbp_paths_and_seasons:
        cols = ["receiver","week","yards_after_catch","xyac_mean_yardage","play_type","season_type","complete_pass"]
        p = pd.read_parquet(path, columns=cols)
        p = p[(p.season_type=="REG") & (p.play_type=="pass") & (p.complete_pass==1)].dropna(subset=["receiver"])
        p["season"] = season
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)
    allp["yac_oe"] = allp.yards_after_catch - allp.xyac_mean_yardage
    lb = allp.groupby("receiver").agg(yac_oe=("yac_oe","mean"), targets=("yac_oe","size")).reset_index()
    lb = lb[lb.targets >= min_targets].sort_values("yac_oe", ascending=False)
    weekly = allp.groupby(["receiver","season","week"]).agg(yac_oe=("yac_oe","mean"), catches=("yac_oe","size")).reset_index()
    return lb, weekly

def build_rusher_epa(pbp_paths_and_seasons, min_carries=30):
    """Season-long rusher leaderboard: rushing EPA/play (not 'over expected' -- no public
    expected-rush-yards model exists in this data, so this is a real but different metric)."""
    frames = []
    for path, season in pbp_paths_and_seasons:
        cols = ["rusher","week","epa","play_type","season_type"]
        p = pd.read_parquet(path, columns=cols)
        p = p[(p.season_type=="REG") & (p.play_type=="run")].dropna(subset=["rusher"])
        p["season"] = season
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)
    lb = allp.groupby("rusher").agg(rush_epa=("epa","mean"), carries=("epa","size")).reset_index()
    lb = lb[lb.carries >= min_carries].sort_values("rush_epa", ascending=False)
    weekly = allp.groupby(["rusher","season","week"]).agg(rush_epa=("epa","mean"), carries=("epa","size")).reset_index()
    return lb, weekly
