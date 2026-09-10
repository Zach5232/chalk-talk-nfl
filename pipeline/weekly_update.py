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

# Firestore write path -- optional import so a local "just show me the numbers" run still
# works with zero setup even without firebase-admin installed or credentials configured.
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False

# SEASON/WEEK: env-var overridable (CHALKTALK_SEASON / CHALKTALK_WEEK) so the GitHub Actions
# "Run workflow" button can target a different week with no code edit or push required -- the
# literals below are just the defaults for a bare local run. The scheduled cron trigger has no
# way to pass inputs, so it always uses these defaults; bump them here specifically to change
# what the unattended weekly run targets.
SEASON = int(os.environ.get("CHALKTALK_SEASON", 2026))   # real season, kicks off 2026-09-09
WEEK = int(os.environ.get("CHALKTALK_WEEK", 1))           # Week 1 -- no 2026 games played yet,
                        # ratings run purely off the carryover-from-2025 prior (see run_ratings:
                        # hist is empty for week 1, so off/deft = prior_off/prior_def directly)

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

# All scratch/cache files (downloaded pbp, games.csv, odds/weather json dumps) live under a
# directory next to this script, not a hardcoded /home/claude path -- so this pipeline runs
# identically on a bare GitHub Actions runner as it does anywhere else. Ephemeral by design:
# a CI runner starts empty every run, so nothing here needs to survive between runs.
PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(PIPELINE_DIR, "_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def fetch_games_csv():
    """Real, always-current nflverse schedule/results dataset -- every game ever played, plus
    the full scheduled slate for the current season with real scores filled in as they
    finish. Re-downloaded on every call (small file, cheap) rather than assumed to exist on
    disk already -- same reasoning as fetch_pbp() below."""
    path = os.path.join(CACHE_DIR, "games.csv")
    url = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
    code = subprocess.run(["curl", "-sL", "-o", path, "-w", "%{http_code}", url],
                           capture_output=True, text=True).stdout.strip()
    if code != "200":
        raise RuntimeError(f"Failed to download games.csv from nflverse (HTTP {code}).")
    return path


# ---------- STEP 1: play-by-play -> team-game EPA splits ----------
def fetch_pbp(season):
    """
    Returns the local path to that season's pbp parquet, or None if nflverse hasn't
    published it yet -- true for the CURRENT season before any games have been played
    (e.g. the day before Week 1 kicks off). curl with just -sL still exits 0 on a 404 and
    writes the error page to the file, so this checks the real HTTP status instead of
    trusting the exit code, and never silently hands back a bad file.
    """
    path = os.path.join(CACHE_DIR, f"pbp_{season}.parquet")
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
        g = pd.read_csv(fetch_games_csv())
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
    games = pd.read_csv(fetch_games_csv())
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
    games = pd.read_csv(fetch_games_csv())
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
    out_path = os.path.join(CACHE_DIR, "week_odds.json")
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
sys.path.insert(0, PIPELINE_DIR)
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
        out_path = os.path.join(CACHE_DIR, f"wx_{r.home_team}_{r.week}.json")
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

    games = pd.read_csv(fetch_games_csv())
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


# ---------- Firestore write path (GitHub Actions cron + manual "Run workflow" trigger) ----------
# Real write, no local paste-into-ChalkTalk.html step anymore: this is what replaces the old
# "copy the printed JSON blocks into the file by hand" workflow. The site reads all of this
# live via the Firebase JS SDK -- see firestore-schema.md for the exact doc shapes this
# writes, which the site's loader expects verbatim.
def get_firestore_client(cred_path):
    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def write_firestore(db, *, season, week, ratings_rows, games, books, closing, weather,
                     rating_history, qb_leaderboard, wr_leaderboard, rb_leaderboard,
                     qb_history, team_players, fantasy_projections, underperformance_report=None):
    """One real write per real thing computed this run. Batches where Firestore allows it
    (500-write cap per batch, nowhere close to hit here); ratings_history/leaderboards/
    fantasy_projections/meta are each a single doc, so those are plain sets."""
    written = []

    # ratings/{team} -- one doc per team, this week's snapshot
    batch = db.batch()
    for r in ratings_rows:
        doc = {**r, "week": week, "season": season,
               "updated_at": firestore.SERVER_TIMESTAMP}
        batch.set(db.collection("ratings").document(r["team"]), doc)
    batch.commit()
    written.append(f"ratings/* ({len(ratings_rows)} teams)")

    # ratings_history/{team} -- run_rating_history() recomputes the FULL walk-forward history
    # from week 1 through `week` every time, so this is a plain overwrite, not a merge.
    batch = db.batch()
    for team, weeks in rating_history.items():
        batch.set(db.collection("ratings_history").document(team), {"team": team, "weeks": weeks})
    batch.commit()
    written.append(f"ratings_history/* ({len(rating_history)} teams)")

    # games/{season}-wk{week} -- one doc, this week's full slate
    db.collection("games").document(f"{season}-wk{week}").set({
        "season": season, "week": week, "games": games,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    written.append(f"games/{season}-wk{week} ({len(games)} games)")

    # books/{gameId} -- one doc per game this week
    batch = db.batch()
    for gid, entry in books.items():
        batch.set(db.collection("books").document(gid), entry)
    batch.commit()
    written.append(f"books/* ({len(books)} games)")

    # weather/{gameId} -- one doc per game this week
    batch = db.batch()
    for gid, entry in weather.items():
        batch.set(db.collection("weather").document(gid), entry)
    batch.commit()
    written.append(f"weather/* ({len(weather)} games)")

    # closing_results/{gameId} -- set (not overwrite-the-collection), so this naturally
    # accumulates across weeks: each week's real gameIds land as their own new docs
    # alongside every prior week's, nothing gets clobbered.
    if closing:
        batch = db.batch()
        for gid, entry in closing.items():
            batch.set(db.collection("closing_results").document(gid), entry)
        batch.commit()
        written.append(f"closing_results/* ({len(closing)} games, previous week + any current-week finals)")

    # leaderboards/current + fantasy_projections/current -- only written when this run
    # actually had real pbp to compute them from (main() passes None otherwise), so an
    # early-season run with no pbp yet never clobbers good data with an empty result.
    if qb_leaderboard is not None:
        db.collection("leaderboards").document("current").set({
            "qb": qb_leaderboard, "wr": wr_leaderboard, "rb": rb_leaderboard,
            "qb_history": qb_history, "team_players": team_players,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        written.append("leaderboards/current")

    if fantasy_projections:
        db.collection("fantasy_projections").document("current").set({
            "projections": fantasy_projections, "updated_at": firestore.SERVER_TIMESTAMP,
        })
        written.append("fantasy_projections/current")

    # fantasy_underperformance/current -- real, refreshed every run this pipeline executes, no
    # external ranking source needed. Written even when `players` is empty (e.g. before any
    # current-season games exist) so the dashboard shows the honest "note" instead of stale
    # data from a prior run, or nothing at all.
    if underperformance_report is not None:
        db.collection("fantasy_underperformance").document("current").set({
            **underperformance_report, "updated_at": firestore.SERVER_TIMESTAMP,
        })
        written.append(f"fantasy_underperformance/current ({len(underperformance_report.get('players', []))} players)")

    # meta/current -- tells the live site which games/{weekId} doc is "this week"
    db.collection("meta").document("current").set({
        "season": season, "week": week, "updated_at": firestore.SERVER_TIMESTAMP,
    })
    written.append("meta/current")

    print("\n--- Firestore write complete ---")
    for w in written:
        print(f"  wrote {w}")


def build_model_season_record(db, season):
    """The model's real ATS record against every real closing line, for every game this
    season -- not scoped to what you personally picked or bet (that's Pick'em/Survivor/Bets,
    already tracked separately). Reads back every games/{season}-wk* doc plus every real
    closing_results entry that already accumulates automatically every week (never
    overwritten), so this is a real, growing season-long scoreboard with no new data
    collection needed -- just tying together two things that were already being saved.

    Grading uses the EXACT same convention as gradeATSPick() in ChalkTalk.html (margin =
    home_score - away_score; diff = margin - close_home; push if diff==0; home covers if
    diff>0) so this can never silently disagree with what the dashboard shows for an
    individual pick -- same math, just applied to every game instead of only picked ones.
    Grading is against the REAL closing line (closing_results['close_home'], straight from
    nflverse's own spread_line) rather than whatever the live odds board showed at write-time --
    that live "market" field can go null for a game that's already kicked off or finished by
    the time a run happens (the odds API stops quoting it), which would otherwise wrongly skip
    grading a real, already-decided game. Games with no closing_results entry yet (not final)
    are skipped, not graded with a fabricated side.
    """
    games_docs = db.collection("games").stream()
    closing_docs = {d.id: d.to_dict() for d in db.collection("closing_results").stream()}

    graded, by_week = [], {}
    for doc in games_docs:
        gdoc = doc.to_dict() or {}
        if gdoc.get("season") != season:
            continue
        wk = gdoc.get("week")
        for g in gdoc.get("games", []):
            cr = closing_docs.get(g.get("id"))
            if not cr:
                continue
            model_home_favored = -g["model"]
            market_home_favored = cr["close_home"]
            edge = model_home_favored - market_home_favored
            model_side = "home" if edge > 0 else "away"
            picked_team = g["home"] if model_side == "home" else g["away"]

            margin = cr["home_score"] - cr["away_score"]
            diff = margin - cr["close_home"]
            if abs(diff) < 1e-9:
                grade = "push"
            else:
                home_covered = diff > 0
                grade = "win" if home_covered == (model_side == "home") else "loss"

            row = {"week": wk, "game_id": g["id"], "away": g["away"], "home": g["home"],
                   "model_side": picked_team, "edge": round(abs(edge), 2), "grade": grade}
            graded.append(row)
            by_week.setdefault(wk, {"wins": 0, "losses": 0, "pushes": 0})
            by_week[wk][{"win": "wins", "loss": "losses", "push": "pushes"}[grade]] += 1

    wins = sum(1 for r in graded if r["grade"] == "win")
    losses = sum(1 for r in graded if r["grade"] == "loss")
    pushes = sum(1 for r in graded if r["grade"] == "push")
    win_pct = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else None

    return {
        "season": season, "wins": wins, "losses": losses, "pushes": pushes, "win_pct": win_pct,
        "total_graded": len(graded), "by_week": by_week, "games": graded,
    }


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

def build_team_top_players(pbp_paths_and_seasons, min_carries=15, min_targets=10):
    """Per-team snapshot: current-ish starting QB (most pass attempts for that team in the
    MOST RECENT season present in pbp_paths_and_seasons -- a real but imperfect proxy; it
    reflects who started last, not necessarily who's QB1 today if there's been an offseason
    change nflverse pbp can't see yet), top rusher by rush EPA/play, top receiver by YAC-over-
    expected, and team pass-block context (sack rate, QB-hit rate, both as a fraction of real
    dropbacks). All real, all from real play-by-play -- no fabricated numbers, but every field
    here is a season-to-date/last-season snapshot, not a live depth chart."""
    cols = ["posteam","passer","rusher","receiver","epa","yards_after_catch","xyac_mean_yardage",
            "play_type","season_type","complete_pass","sack","qb_hit"]
    frames = []
    for path, season in pbp_paths_and_seasons:
        p = pd.read_parquet(path, columns=cols)
        p = p[p.season_type == "REG"].copy()
        p["season"] = season
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)

    out = {}
    for team in sorted(allp.posteam.dropna().unique()):
        tp = allp[allp.posteam == team]
        latest_season = tp.season.max()

        qb_pool = tp[(tp.season == latest_season) & (tp.play_type == "pass") & tp.passer.notna()]
        qb = qb_pool.passer.value_counts().idxmax() if len(qb_pool) else None
        # Every real passer this team has used, across all seasons in the window -- excluded
        # from "top rusher"/"top receiver" below. Without this, a QB's scramble EPA (small
        # sample, often garbage-time/broken-play) can look like an elite rushing season and
        # wrongly surface as the team's top rusher (caught this for real: J.Flacco was coming
        # back as CIN's "top rusher" off a handful of scrambles before this filter).
        team_passers = set(tp[tp.play_type == "pass"].passer.dropna().unique())

        rush_pool = tp[(tp.play_type == "run") & tp.rusher.notna() & ~tp.rusher.isin(team_passers)]
        rstats = rush_pool.groupby("rusher").agg(epa=("epa", "mean"), n=("epa", "size"))
        rstats = rstats[rstats.n >= min_carries]
        top_rusher = None
        if len(rstats):
            name = rstats.epa.idxmax()
            top_rusher = {"name": name, "epa": round(float(rstats.loc[name, "epa"]), 3)}

        rec_pool = tp[(tp.play_type == "pass") & (tp.complete_pass == 1) & tp.receiver.notna()
                      & ~tp.receiver.isin(team_passers)].copy()
        rec_pool["yac_oe"] = rec_pool.yards_after_catch - rec_pool.xyac_mean_yardage
        cstats = rec_pool.groupby("receiver").agg(yac_oe=("yac_oe", "mean"), n=("yac_oe", "size"))
        cstats = cstats[cstats.n >= min_targets]
        top_receiver = None
        if len(cstats):
            name = cstats.yac_oe.idxmax()
            top_receiver = {"name": name, "yac_oe": round(float(cstats.loc[name, "yac_oe"]), 2)}

        pass_pool = tp[tp.play_type == "pass"]
        n_pass = len(pass_pool)
        sack_rate = round(float(pass_pool.sack.sum()) / n_pass, 3) if n_pass else None
        hit_rate = round(float(pass_pool.qb_hit.sum()) / n_pass, 3) if n_pass else None

        out[team] = {"qb": qb, "top_rusher": top_rusher, "top_receiver": top_receiver,
                     "sack_rate": sack_rate, "hit_rate": hit_rate}
    return out

def _fetch_stats_player_reg(yr):
    """Real per-player season stats file for one season, or None if nflverse hasn't published
    it yet (true for a season before any games have been played). Shared by
    build_fantasy_projections and build_underperformance_report so both use the exact same
    fetch logic, not two copies that could drift."""
    import urllib.request
    url = f"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_reg_{yr}.parquet"
    try:
        path = f"/tmp/stats_player_reg_{yr}.parquet"
        urllib.request.urlretrieve(url, path)
        df = pd.read_parquet(path)
        if len(df) == 0:
            return None
        return df
    except Exception:
        return None

_SURNAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
def _surname_key(full_name):
    # Token-based suffix stripping (not substring replace) -- a substring approach would
    # wrongly mangle real surnames that happen to contain "ii"/"sr" as letters within them.
    tokens = [t.rstrip(".").lower() for t in str(full_name).split()]
    while tokens and tokens[-1] in _SURNAME_SUFFIXES:
        tokens.pop()
    return tokens[-1] if tokens else ""

def build_underperformance_report(season, min_current_games=3):
    """Flags real players whose CURRENT real season production is meaningfully below their
    OWN established real baseline (last full real season) -- no external ranking source
    needed, so this has no staleness problem the way a preseason ranking snapshot would:
    it's real data, refreshed every time this pipeline runs.

    Deliberately does NOT invent a "significance" threshold for what counts as a real
    decline (that would be an unvalidated number dressed up as a rule) -- it reports the
    real baseline ppg, the real current ppg, and the real gap, sorted worst-gap-first, and
    lets the dashboard show that honestly rather than a fabricated "underperforming: yes/no"
    verdict. min_current_games guards against a 1-2 game sample looking like a real trend
    when it's just noise -- returns an empty dict (not a fabricated report) until real games
    reach that floor.
    """
    baseline_df = _fetch_stats_player_reg(season - 1)
    current_df = _fetch_stats_player_reg(season)
    if baseline_df is None or current_df is None:
        missing = ([f"{season-1} baseline"] if baseline_df is None else []) + \
                  ([f"{season} current-season"] if current_df is None else [])
        return {"players": [], "baseline_season": season - 1, "current_season": season,
                "note": f"Not available yet: {' and '.join(missing)} stats."}

    keep_pos = {"QB", "RB", "WR", "TE"}
    def to_ppg_map(df, min_games):
        df = df[(df.games >= min_games) & (df.position.isin(keep_pos))].copy()
        df["half_ppr"] = (df["fantasy_points"] + df["fantasy_points_ppr"]) / 2
        df["ppg"] = df["half_ppr"] / df["games"]
        out = {}
        for _, r in df.iterrows():
            key = f"{str(r['recent_team']).lower()}|{_surname_key(r['player_display_name'])}"
            out[key] = {"ppg": round(float(r["ppg"]), 2), "games": int(r["games"]),
                        "pos": r["position"], "name": r["player_display_name"], "team": r["recent_team"]}
        return out

    baseline = to_ppg_map(baseline_df, min_games=5)  # need a real, stable prior-season sample
    current = to_ppg_map(current_df, min_games=min_current_games)

    players = []
    for key, cur in current.items():
        base = baseline.get(key)
        if not base:
            continue  # no real established baseline to compare against -- skip, don't guess
        gap = round(cur["ppg"] - base["ppg"], 2)
        players.append({
            "name": cur["name"], "pos": cur["pos"], "team": cur["team"],
            "baseline_ppg": base["ppg"], "baseline_games": base["games"],
            "current_ppg": cur["ppg"], "current_games": cur["games"],
            "gap": gap,
        })
    players.sort(key=lambda p: p["gap"])  # worst decline first
    return {"players": players, "baseline_season": season - 1, "current_season": season, "note": None}

def build_fantasy_projections(season):
    """'Our Proj' for every rostered/waiver player: real half-PPR season-average fantasy
    points per game. Uses nflverse's own official fantasy_points (standard) and
    fantasy_points_ppr (full PPR) season-total columns from stats_player_reg_{season}.parquet
    -- half-PPR is exactly the midpoint of those two since receptions are the only scoring
    term that differs between them, so (standard + full_ppr) / 2 is an exact derivation, not
    an approximation.

    Backtested finding (see fantasy-integration.md): plain season-to-date average beat every
    fancier projection approach tried (recency-weighting, defense-vs-position adjustment) --
    MAE 4.26 vs 4.29-4.36. So this intentionally stays simple rather than adding a signal that
    already tested worse.

    Real limitation, stated plainly: this is a REAL SEASON prior (current season if any games
    have been played yet, else the most recently completed season as a carryover prior, same
    idea as the team ratings' carryover) -- it is not a lookahead, but it's also not adjusted
    for this week's specific opponent or a player's role change since. Keyed by
    team|lastname (lowercased) since roster display names vary in format (full name vs.
    abbreviated) across Sleeper/ESPN/Yahoo -- team+lastname is unique enough in practice for
    an active-roster skill player, with the rare same-team-same-lastname collision an accepted
    known limitation of this approach."""
    df = _fetch_stats_player_reg(season)
    used_season = season
    if df is None or len(df) == 0:
        df = _fetch_stats_player_reg(season - 1)
        used_season = season - 1
    if df is None:
        return {}, None

    # K deliberately excluded: nflverse's fantasy_points/fantasy_points_ppr columns only cover
    # offensive skill-position scoring, not kicking (FG/PAT) -- every kicker was coming back as
    # a real-looking 0.0 projection, which is a false number (not "we project 0 points"), not a
    # true zero. Caught this checking real output before shipping. No real fix without pulling
    # in FG/PAT stats separately and building actual kicker scoring -- not done here, so K (and
    # D/ST, which was never in this player-level file to begin with) stay unprojected/"--" in
    # the UI rather than showing a fabricated number.
    keep_pos = {"QB", "RB", "WR", "TE"}
    df = df[(df.games > 0) & (df.position.isin(keep_pos))].copy()
    df["half_ppr"] = (df["fantasy_points"] + df["fantasy_points_ppr"]) / 2
    df["ppg"] = df["half_ppr"] / df["games"]

    out = {}
    for _, r in df.iterrows():
        last = _surname_key(r["player_display_name"])
        key = f"{str(r['recent_team']).lower()}|{last}"
        out[key] = {"proj": round(float(r["ppg"]), 2), "games": int(r["games"]), "pos": r["position"]}
    return out, used_season


if __name__ == "__main__":
    print(f"=== Chalk Talk weekly update: season {SEASON}, week {WEEK} ({MODE} mode) ===\n")

    ratings = run_ratings(SEASON, WEEK, prior_season_pbp_path="/home/claude/odds_pull/pbp_2024.parquet"
                           if SEASON == 2025 else fetch_pbp(SEASON - 1))

    odds_data = pull_week_odds(MODE, API_KEY, HIST_DATE)
    books = build_books_for_week(odds_data, ratings["games_this_week"])
    # Grade previous week's games (normal weekly cadence) PLUS any game in the CURRENT
    # week's slate that has already gone final -- e.g. re-running mid-week after a
    # Thursday/Sunday-night opener finishes, without waiting for the whole week to end.
    closing = build_closing_results(pd.concat([ratings["games_prev_week"], ratings["games_this_week"]]))
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
            "id": gid, "away": a, "home": h, "week": WEEK, "season": SEASON,
            # gameday/gametime come straight from the real nflverse schedule (games.csv) --
            # gametime is already ET, same as every other time shown in this dashboard.
            "gameday": str(r.gameday) if pd.notna(r.gameday) else None,
            "gametime": str(r.gametime) if pd.notna(r.gametime) else None,
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

    # ---- Player-level: QB_LEADERBOARD / WR_LEADERBOARD / RB_LEADERBOARD / QB_HISTORY /
    # TEAM_PLAYERS. Real functions existed in this file (build_qb_leaderboard etc.) but were
    # never actually called from main() -- dead code, silently never producing real output.
    # Wired in here. Needs real current-season pbp to mean anything current; with none yet
    # (pre-Week-1), this intentionally falls back to the most recent completed real seasons
    # (2024+2025) available locally so the numbers are real, just not 2026-current -- refresh
    # this block specifically once 2026 pbp exists (a few real weeks in).
    import glob, re
    pbp_local = []
    for path in sorted(glob.glob(os.path.join(CACHE_DIR, "pbp_*.parquet"))):
        m = re.search(r"pbp_(\d{4})\.parquet", path)
        if not m:
            continue
        yr = int(m.group(1))
        try:
            if pd.read_parquet(path, columns=["season"]).shape[0] == 0:
                continue  # empty placeholder (e.g. pbp_2026.parquet pre-season)
        except Exception:
            continue
        pbp_local.append((path, yr))

    if pbp_local:
        most_recent_season = max(yr for _, yr in pbp_local)

        qb_lb = build_qb_leaderboard(pbp_local, min_attempts=100)
        qb_lb_out = [{"player": r.passer, "cpoe": round(float(r.cpoe), 2),
                      "epa": round(float(r.epa_per_dropback), 3), "n": int(r.attempts)}
                     for r in qb_lb.head(15).itertuples()]
        print(f"\n--- QB_LEADERBOARD (real, {'+'.join(str(y) for _,y in pbp_local)} combined -- paste into const QB_LEADERBOARD = [ ... ]) ---")
        print(json.dumps(qb_lb_out, indent=1))

        rec_lb, _ = build_receiver_yac_oe(pbp_local, min_targets=20)
        wr_lb_out = [{"player": r.receiver, "yac_oe": round(float(r.yac_oe), 2), "n": int(r.targets)}
                     for r in rec_lb.head(15).itertuples()]
        print(f"\n--- WR_LEADERBOARD (real, {'+'.join(str(y) for _,y in pbp_local)} combined -- paste into const WR_LEADERBOARD = [ ... ]) ---")
        print(json.dumps(wr_lb_out, indent=1))

        rush_lb, _ = build_rusher_epa(pbp_local, min_carries=30)
        rb_lb_out = [{"player": r.rusher, "epa": round(float(r.rush_epa), 3), "n": int(r.carries)}
                     for r in rush_lb.head(15).itertuples()]
        print(f"\n--- RB_LEADERBOARD (real, {'+'.join(str(y) for _,y in pbp_local)} combined -- paste into const RB_LEADERBOARD = [ ... ]) ---")
        print(json.dumps(rb_lb_out, indent=1))

        # QB_HISTORY: within-season week-by-week trend, most recent completed season only
        # (mixing week numbers across seasons on one chart would be misleading).
        qb_wk = build_qb_weekly_history([(p, y) for p, y in pbp_local if y == most_recent_season])
        qb_wk = qb_wk[qb_wk.passer.isin(qb_lb_out and [r["player"] for r in qb_lb_out] or [])]
        qb_history_out = {}
        for name, grp in qb_wk.groupby("passer"):
            qb_history_out[name] = [{"week": int(w), "cpoe": round(float(c), 2)}
                                     for w, c in zip(grp.week, grp.cpoe)]
        print(f"\n--- QB_HISTORY (real, {most_recent_season} only -- paste into const QB_HISTORY = { '{' } ... { '}' }) ---")
        print(json.dumps(qb_history_out, indent=1))

        team_players_out = build_team_top_players(pbp_local)
        print(f"\n--- TEAM_PLAYERS (real, {'+'.join(str(y) for _,y in pbp_local)}, 'qb' reflects {most_recent_season}'s most-used passer per team -- paste into const TEAM_PLAYERS = { '{' } ... { '}' }) ---")
        print(json.dumps(team_players_out, indent=1))
    else:
        print("\n--- QB_LEADERBOARD / WR_LEADERBOARD / RB_LEADERBOARD / QB_HISTORY / TEAM_PLAYERS ---")
        print("No real pbp available locally (checked " + CACHE_DIR + "/pbp_*.parquet) -- skipped. "
              "These need at least one real completed season's play-by-play on disk.")
        qb_lb_out = wr_lb_out = rb_lb_out = qb_history_out = team_players_out = None

    # ---- FANTASY_PROJECTIONS: real half-PPR season-average points, keyed by team|lastname.
    # See build_fantasy_projections() docstring for the exact methodology and its honest
    # limitations. Paste as a new top-level const; ChalkTalk.html looks players up in this
    # table via getProjection(p) instead of relying on a static field, so it covers roster
    # players AND any waiver pickup automatically.
    proj, proj_season = build_fantasy_projections(SEASON)
    print(f"\n--- FANTASY_PROJECTIONS (real, {proj_season} season average, half-PPR -- paste into const FANTASY_PROJECTIONS = { '{' } ... { '}' }) ---")
    print(json.dumps(proj, indent=1))

    # Real, refreshed every run -- no external ranking source, no staleness problem. Flags
    # players whose real current-season production is below their own real established
    # baseline. See build_underperformance_report() docstring for exactly what it does and
    # doesn't do (no invented "significant" threshold, no fabricated verdict).
    underperf = build_underperformance_report(SEASON)
    print(f"\n--- UNDERPERFORMANCE REPORT ({len(underperf['players'])} players flagged"
          f"{', note: ' + underperf['note'] if underperf['note'] else ''}) ---")

    # ---- Write everything real above straight to Firestore -- the actual replacement for
    # the old "paste these JSON blocks into ChalkTalk.html by hand" step. Only runs when
    # credentials are actually configured (the GitHub Actions secret, or a local
    # GOOGLE_APPLICATION_CREDENTIALS/FIREBASE_CREDENTIALS_PATH env var pointing at a real
    # service-account key), so a bare local run with no setup still just prints the numbers
    # like it always has -- nothing breaks for a quick manual sanity check.
    cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if _FIREBASE_AVAILABLE and cred_path:
        fdb = get_firestore_client(cred_path)
        write_firestore(
            fdb, season=SEASON, week=WEEK, ratings_rows=rows_sorted_now, games=games_out,
            books=books, closing=closing, weather=weather, rating_history=history,
            qb_leaderboard=qb_lb_out, wr_leaderboard=wr_lb_out, rb_leaderboard=rb_lb_out,
            qb_history=qb_history_out, team_players=team_players_out,
            fantasy_projections=proj, underperformance_report=underperf,
        )

        # Real season-long model-vs-market record, every game, regardless of what was
        # actually bet/picked -- reads back what was just written above plus every prior
        # week's games/closing_results (both already accumulate automatically), so this
        # naturally grows correctly with zero extra data collection.
        record = build_model_season_record(fdb, SEASON)
        fdb.collection("model_season_record").document("current").set({
            **record, "updated_at": firestore.SERVER_TIMESTAMP,
        })
        print(f"\n--- MODEL SEASON RECORD: {record['wins']}-{record['losses']}"
              f"{'-'+str(record['pushes']) if record['pushes'] else ''} ATS"
              f" ({record['win_pct']}%) across {record['total_graded']} graded games ---")
    else:
        reason = "firebase-admin not installed" if not _FIREBASE_AVAILABLE else "no credentials configured (FIREBASE_CREDENTIALS_PATH / GOOGLE_APPLICATION_CREDENTIALS)"
        print(f"\n(Skipped Firestore write -- {reason}. Numbers above are still real, just not persisted this run.)")


