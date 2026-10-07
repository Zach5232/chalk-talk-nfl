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
import subprocess, json, csv, os, time
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

# SEASON: env-var overridable (CHALKTALK_SEASON), defaults to the real current season. Only
# needs touching once a year at the actual season boundary.
SEASON = int(os.environ.get("CHALKTALK_SEASON", 2026))   # real season, kicks off 2026-09-09

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


def _curl_with_retries(url, path, max_attempts=3, backoff_seconds=3):
    """Downloads url to path, retrying on transient failures (5xx, connection resets, curl
    timeouts) with a short backoff -- caught 2026-09-14, when a real GitHub Actions run hit
    two straight HTTP 504s from nflverse's release CDN on a URL that was verifiably up (curled
    fine, twice, from an unrelated machine at the same moment) -- a real but transient blip,
    not an outage. A definitive 404 (the file genuinely doesn't exist -- true for the current
    season's pbp before it's published) returns immediately; no point retrying that."""
    code = None
    for attempt in range(max_attempts):
        code = subprocess.run(["curl", "-sL", "-o", path, "-w", "%{http_code}", url],
                               capture_output=True, text=True).stdout.strip()
        if code in ("200", "404"):
            return code
        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds * (attempt + 1))
    return code


def fetch_games_csv():
    """Real, always-current nflverse schedule/results dataset -- every game ever played, plus
    the full scheduled slate for the current season with real scores filled in as they
    finish. Re-downloaded on every call (small file, cheap) rather than assumed to exist on
    disk already -- same reasoning as fetch_pbp() below.

    Real bug caught 2026-10-07: the plain uncompressed games.csv release asset started 404ing
    (confirmed independently -- curl against it directly, from two unrelated contexts, same
    404 -- not a transient CDN blip like _curl_with_retries guards against) while games.csv.gz
    at the same release keeps working fine, so nflverse appears to have dropped the
    uncompressed asset. Downloads the real .gz instead and decompresses it to the exact same
    local games.csv path every existing caller (pd.read_csv(fetch_games_csv()), scattered
    throughout this file and every satellite script) already expects -- zero call sites needed
    to change."""
    import gzip, shutil
    path = os.path.join(CACHE_DIR, "games.csv")
    gz_path = path + ".gz"
    url = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv.gz"
    code = _curl_with_retries(url, gz_path)
    if code != "200":
        raise RuntimeError(f"Failed to download games.csv.gz from nflverse (HTTP {code}) after retries.")
    with gzip.open(gz_path, "rb") as f_in, open(path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return path


def _detect_current_week(season):
    """The earliest real week this season that still has at least one game without a final
    result -- i.e. whichever week is upcoming or in progress right now. Once every game in a
    week goes final (Monday night's over), this naturally advances to the next week on its
    own -- no more manually typing a week number into the GitHub Actions "Run workflow" form
    every week, and the unattended Tuesday cron (which has no way to take that input at all)
    now tracks the real season correctly forever, with zero manual maintenance. Falls back to
    the season's last real week once the whole season is actually done."""
    games = pd.read_csv(fetch_games_csv())
    g = games[(games.season.astype(str) == str(season)) & (games.game_type == "REG")]
    incomplete = g[g.result.isna()]
    if len(incomplete):
        return int(incomplete.week.min())
    return int(g.week.max())


# WEEK: CHALKTALK_WEEK still works as a manual override (e.g. to re-run or fix an old week),
# but the real default is now auto-detected from the actual schedule above -- see
# _detect_current_week()'s docstring for why.
WEEK = int(os.environ["CHALKTALK_WEEK"]) if os.environ.get("CHALKTALK_WEEK") else _detect_current_week(SEASON)


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
    code = _curl_with_retries(url, path)
    if code == "404":
        return None  # genuinely not published yet -- expected, not an error
    if code != "200":
        # A persistent transient failure here must NOT be treated the same as "not published
        # yet" -- silently returning None would make run_ratings() quietly fall back to a
        # thinner prior instead of using real, actually-available data, a wrong-output bug
        # that's worse than just failing loudly.
        raise RuntimeError(f"Failed to download pbp for {season} from nflverse (HTTP {code}) after retries.")
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


# ---------- QB status overrides: known-this-week starter changes the season-long rating ----------
# The walk-forward rating above already handles a new starter correctly ONCE it has real snaps
# to learn from -- the problem is the game happening THIS week, before any of those snaps exist.
# A backup/new starter with real in-sample games already (e.g. someone who's started the last
# two weeks and performed fine) needs no help; the rating already reflects their real play. This
# only matters for a starter making their first real start this week.
#
# Schema is keyed by WHO is playing, not just on/off: {team: {active, qb_name, reason,
# penalty_epa (optional manual override)}}. Given a qb_name, the real number is looked up from
# that specific player's own actual EPA/play (qb_personal_penalty, defined above run_ratings) --
# not a flat guess -- so changing who's on the field (e.g. a announced backup ALSO goes down
# during the week and a third-stringer takes over instead) is a one-field edit on the dashboard,
# not a re-derived number. penalty_epa is only there as a manual escape hatch for a genuine
# no-real-track-record case (a rookie/UDFA making a first-ever NFL appearance) -- BACKUP_QB_EPA_
# PENALTY is what qb_personal_penalty's caller falls back to when there's truly no usable real
# data on the named player at all, not a default for anyone with a real history (Firestore is
# public-read, so this is a plain unauthenticated GET, no credentials needed -- consistent with
# every other dashboard_state read/write in this app).
BACKUP_QB_EPA_PENALTY = -0.123

def fetch_qb_status_overrides(season, current_season_pbp_path):
    import urllib.request
    url = "https://firestore.googleapis.com/v1/projects/stock-model-42fb2/databases/(default)/documents/dashboard_state/qb_status_overrides"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            doc = json.loads(resp.read())
    except Exception as e:
        print(f"  (qb_status_overrides: couldn't fetch, skipping -- {e})")
        return {}
    fields = doc.get("fields", {}).get("value", {}).get("mapValue", {}).get("fields", {})
    out = {}
    changed = False
    for team, entry in fields.items():
        f = entry.get("mapValue", {}).get("fields", {})
        active = f.get("active", {}).get("booleanValue", False)
        if not active:
            continue
        qb_name = f.get("qb_name", {}).get("stringValue") or None
        manual_penalty = f.get("penalty_epa", {}).get("doubleValue")
        reason = f.get("reason", {}).get("stringValue", "") or f.get("note", {}).get("stringValue", "")

        if manual_penalty is not None:
            penalty, source = float(manual_penalty), "manual override"
        elif qb_name:
            computed = qb_personal_penalty(qb_name, season, current_season_pbp_path)
            penalty, source = (computed, "real personal EPA") if computed is not None else (BACKUP_QB_EPA_PENALTY, "generic fallback -- no real data on this player")
        else:
            penalty, source = BACKUP_QB_EPA_PENALTY, "generic fallback -- no qb_name given"

        out[team] = penalty
        who = f" ({qb_name})" if qb_name else ""
        print(f"  QB status override active: {team}{who} = {penalty:+.3f} EPA/play [{source}] -- {reason}")

        # Write the real computed number back onto the doc (informational only -- never read
        # back in as an input) so the dashboard can show what was actually used, instead of
        # the person having to trust a number they can't see.
        f["computed_penalty_epa"] = {"doubleValue": round(penalty, 4)}
        f["computed_source"] = {"stringValue": source}
        changed = True

    if changed:
        try:
            import urllib.request as _ur
            body = json.dumps({"fields": {"value": {"mapValue": {"fields": fields}}}}).encode()
            req = _ur.Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
            _ur.urlopen(req, timeout=15)
        except Exception as e:
            print(f"  (qb_status_overrides: couldn't write computed values back, non-fatal -- {e})")
    return out


# ---------- Automated QB depth-chart detection -------------------------------------------
# Real problem this replaces: qb_status_overrides (above) only updates when a person notices a
# real starter change AND manually types it in -- confirmed stale in practice (a real mid-week
# change sat unflagged on the dashboard with no one around to catch it). Two real, free nflverse
# feeds make this automatable with no Odds API cost at all:
#   - depth_charts: official team-reported depth charts, refreshed ~2x/real-day all season
#   - injuries: official weekly injury report (report_status: Out/Doubtful/Questionable/...)
# Neither one alone is reliable (a depth chart can leave an injured starter listed at #1 for
# real cosmetic/historical reasons -- confirmed live: CHI's depth chart still lists Caleb
# Williams #1 while the real injury report has him real-"Out" with a hamstring injury three
# real weeks running, and the real games were started by Bagent instead). So this cross-checks
# both real sources against each other, same "don't trust one input blindly" discipline as
# everywhere else in this file.
UNAVAILABLE_INJURY_STATUSES = {"out", "injured reserve", "ir", "doubtful"}

_SURNAME_SUFFIXES_QB = {"jr", "sr", "ii", "iii", "iv", "v"}
def _full_name_to_pbp_short(full_name):
    """'Jacoby Brissett' -> 'J.Brissett', matching nflverse PBP's own short-name convention --
    same best-effort first-initial + last-surname-token approach verified against the real
    weeks 1-4 prop backtest (87% real match rate), now reused here instead of a second copy."""
    toks = str(full_name).strip().split()
    if not toks:
        return None
    first = toks[0]
    rest = toks[1:]
    while len(rest) > 1 and rest[-1].rstrip(".").lower() in _SURNAME_SUFFIXES_QB:
        rest.pop()
    surname = rest[-1] if rest else toks[0]
    return f"{first[0]}.{surname}"


def fetch_depth_charts(season):
    """Real, current depth-chart QB order per team (pos_rank 1, 2, 3, ...), each team's own
    most recent real snapshot (teams don't all update at the same moment) -- or None if
    nflverse hasn't published this season's file yet."""
    import urllib.request
    url = f"https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.parquet"
    try:
        path = f"/tmp/depth_charts_{season}.parquet"
        urllib.request.urlretrieve(url, path)
        df = pd.read_parquet(path, columns=["dt", "team", "player_name", "pos_grp", "pos_abb", "pos_rank"])
    except Exception as e:
        print(f"  (depth_charts: couldn't fetch, skipping auto QB detection -- {e})")
        return None
    qb = df[(df.pos_grp == "3WR 1TE") & (df.pos_abb == "QB")].copy()
    if qb.empty:
        return None
    team_latest_dt = qb.groupby("team")["dt"].transform("max")
    return qb[qb.dt == team_latest_dt].sort_values(["team", "pos_rank"])


def fetch_injury_report(season):
    """Real most-recent-week official injury report, name (lowercased) -> report_status
    (lowercased), or {} if not published yet. Only report_status matters here (Out/Doubtful/
    etc.) -- a player with no row, or a NaN status, is treated as available, same as the real
    injury report itself implies by omission."""
    import urllib.request
    url = f"https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.parquet"
    try:
        path = f"/tmp/injuries_{season}.parquet"
        urllib.request.urlretrieve(url, path)
        df = pd.read_parquet(path, columns=["week", "full_name", "report_status"])
    except Exception as e:
        print(f"  (injuries: couldn't fetch, skipping injury cross-check -- {e})")
        return {}
    if df.empty:
        return {}
    latest_week = df.week.max()
    latest = df[(df.week == latest_week) & df.report_status.notna()]
    return {str(n).lower().strip(): str(s).lower().strip() for n, s in zip(latest.full_name, latest.report_status)}


def build_qb_depth_chart_status(season, current_season_pbp_path):
    """The real automated replacement for manually noticing a QB change -- including a
    starter RECLAIMING the job once they're healthy again, not just losing it.

    Primary signal is the real depth chart itself, walked in rank order past anyone the real
    injury report lists Out/Doubtful/IR -- the team's own official, current declaration of who
    plays, cross-checked against who's actually confirmed unavailable. This replaced an earlier
    version that defaulted to "whoever started the last real game" instead: that version
    correctly caught a starter going DOWN, but then stayed stuck on the backup forever even
    after the real starter recovered (never flagged a recovery, since a healthy backup who
    already played never triggers a change on its own) -- exactly the staleness problem this
    whole system exists to kill, just moved rather than fixed. Depth-chart-primary catches both
    directions the same real way, no human judgment required.

    Only flags a team when this real candidate differs from who actually started last time --
    a stable, healthy starter produces zero noise.

    Known, honest limitation: a real non-injury benching that the depth chart hasn't reordered
    yet and the injury report has no reason to mention (confirmed live: WAS still depth-chart-
    lists Mariota #2 over Kaliakmanis, who's actually the one playing, with neither flagged
    injured at all) won't be caught until the real depth chart itself updates -- no structured
    data source captures a pure coaching decision. Self-corrects within about a real day, given
    the real ~2x/day depth-chart refresh cadence; the manual override below still exists for
    the rare case someone wants it fixed sooner than that.
    """
    dc = fetch_depth_charts(season)
    injuries = fetch_injury_report(season)
    if dc is None:
        return {}

    primary_by_team = _primary_passer_by_team(pd.read_parquet(current_season_pbp_path,
                                                                columns=["play_type", "epa", "posteam", "passer_player_name"]))

    def unavailable(full_name):
        return injuries.get(str(full_name).lower().strip()) in UNAVAILABLE_INJURY_STATUSES

    out = {}
    for team, rows in dc.groupby("team"):
        rows = rows.sort_values("pos_rank")
        healthy = [r.player_name for _, r in rows.iterrows() if not unavailable(r.player_name)]
        candidate_full = healthy[0] if healthy else rows.iloc[0].player_name  # everyone flagged -- fall back to #1 anyway
        candidate_short = _full_name_to_pbp_short(candidate_full)

        last_game_short = primary_by_team.get(team)
        flag = bool(last_game_short) and (candidate_short != last_game_short)

        if not flag:
            out[team] = {"flag": False, "qb_name": candidate_short, "qb_full_name": candidate_full,
                         "reason": "No change from last real game's starter.",
                         "penalty_epa": 0.0, "source": "no change detected"}
            continue

        computed = qb_personal_penalty(candidate_short, season, current_season_pbp_path)
        penalty, source = (computed, "real personal EPA") if computed is not None else (BACKUP_QB_EPA_PENALTY, "generic fallback -- no real data on this player")
        reason = (f"Real depth chart (injury-checked) now has {candidate_full} over last real game's starter ({last_game_short})."
                  if last_game_short else
                  f"No prior real game this season yet -- using the real depth chart's top healthy name, {candidate_full}.")
        out[team] = {"flag": True, "qb_name": candidate_short, "qb_full_name": candidate_full,
                     "reason": reason, "penalty_epa": round(penalty, 4), "source": source}
        print(f"  AUTO QB change detected: {team} -> {candidate_full} ({candidate_short}) = {penalty:+.3f} EPA/play [{source}] -- {reason}")

    return out


def fetch_qb_depth_chart_status_dict(season, current_season_pbp_path):
    """Reads the ALREADY-computed auto status back from Firestore (written by the separate,
    frequent pipeline/qb_status_check.py -- see that file's own docstring for why this is a
    decoupled, more-frequent job rather than folded into this once-a-week pipeline) and
    returns just the {team: penalty_epa} shape fetch_qb_status_overrides also returns, so the
    two merge with a plain dict union at the call site (manual wins on a shared key). Falls
    back to computing it fresh in-process if the doc isn't there yet (e.g. first deploy before
    the new workflow has ever run) rather than silently applying nothing."""
    import urllib.request
    url = "https://firestore.googleapis.com/v1/projects/stock-model-42fb2/databases/(default)/documents/qb_depth_chart_status/current"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            doc = json.loads(resp.read())
        fields = doc.get("fields", {}).get("teams", {}).get("mapValue", {}).get("fields", {})
        if not fields:
            raise ValueError("empty/missing teams map")
        out = {}
        for team, entry in fields.items():
            f = entry.get("mapValue", {}).get("fields", {})
            if f.get("flag", {}).get("booleanValue", False):
                out[team] = f.get("penalty_epa", {}).get("doubleValue", 0.0)
        return out
    except Exception as e:
        print(f"  (qb_depth_chart_status: couldn't read {e}, computing fresh in-process instead)")
        status = build_qb_depth_chart_status(season, current_season_pbp_path)
        return {t: s["penalty_epa"] for t, s in status.items() if s["flag"]}


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


# ---------- Personal QB EPA lookup: powers QB Watch's per-game override (see games_out below) ----
# NOTE: this used to also auto-adjust the season-long prior_off for a team with a new offseason
# starter. Reverted -- a real walk-forward backtest (2024-2025, McNemar-tested) showed it made
# predictions slightly WORSE (ATS 48.9% -> 47.3%, MAE and correlation both worse too), not
# better. The ridge fit already absorbs a real new starter's own snaps within a few weeks on its
# own (that's what halflife=6 is for), and personal career passing EPA turned out to be a noisier
# stand-in for "how will this team's whole offense perform" than it looked on paper. Left here as
# a documented dead end, not a TODO to re-add.
#
# What's still real and still used: given a SPECIFIC named QB, how does their own actual EPA/play
# compare to league average, recency+sample-weighted across their own real seasons. This backs
# QB Watch's per-game injury override (a team's CURRENT starter is confirmed out for THIS week's
# specific game, with a specific real replacement) -- a fundamentally different, event-driven
# correction from the reverted structural one above, and the one thing this session's backtests
# never actually contradicted.
def _passer_epa_vs_league(pbp_df, passer_name, min_attempts):
    """Returns (raw_epa_vs_league_avg, n_attempts), or None below min_attempts. The raw value is
    NOT shrunk here -- callers apply _shrink() themselves, since how much shrinkage is right
    depends on how the result gets used."""
    pass_plays = pbp_df[(pbp_df.play_type == "pass") & pbp_df.epa.notna()]
    league_avg = pass_plays.epa.mean()
    p = pass_plays[pass_plays.passer_player_name == passer_name]
    if len(p) < min_attempts:
        return None
    return p.epa.mean() - league_avg, len(p)

def _primary_passer_by_team(pbp_df):
    pass_plays = pbp_df[(pbp_df.play_type == "pass") & pbp_df.epa.notna() & pbp_df.passer_player_name.notna()]
    counts = pass_plays.groupby(["posteam", "passer_player_name"]).size().reset_index(name="att")
    if len(counts) == 0:
        return {}
    primary = counts.loc[counts.groupby("posteam")["att"].idxmax()]
    return dict(zip(primary["posteam"], primary["passer_player_name"]))

# Real, per-play EPA over a SHORT stretch is extremely noisy -- a couple of pick-sixes or
# strip-sacks in a 40-attempt sample can swing it by half a point per play, which is a bigger
# swing than the gap between the best and worst team in the league. Shrink every personal
# estimate toward 0 by its own sample size (standard n/(n+k) empirical-Bayes shrinkage) before
# it ever gets combined into anything else -- a real, first-attempt version of this WITHOUT
# shrinkage produced an actual -24-point ATL rating off a 44-attempt Cooper Rush sample, which
# is exactly the failure mode this exists to prevent.
_QB_SHRINKAGE_K = 150  # attempts for ~50% trust; 44 attempts -> ~23% trust, 300 -> ~67%
def _shrink(raw, n):
    return raw * n / (n + _QB_SHRINKAGE_K)

def qb_personal_penalty(qb_name, season, current_season_pbp_path):
    """The real, recency+sample-weighted EPA/play-vs-league-average for ONE named QB, across
    their real snaps this season plus their real snaps the 3 seasons before it. Verified against
    a real, independent manual calculation (Jameis Winston, Marcus Mariota, real 2023-2026 data)
    before this was written as reusable code -- a 2-season version (this season + just the one
    immediate prior season, which is all the main model already downloads) was tried first and
    rejected: it missed Winston's two real down years in 2023-2024, understating how much worse
    than average his real track record actually is (-0.009 vs. the real, fuller -0.140). Costs
    2 extra real season downloads at call time -- only happens when a QB Watch entry actually
    names a qb_name with no manual penalty_epa override, not on every regular run. Returns None
    if there's truly no usable real data on them anywhere (never fabricate a number for a total
    unknown -- caller should fall back to a generic assumption or leave the override unquantified).
    """
    # NOTE: deliberately does NOT also apply _shrink()'s per-sample n/(n+k) shrinkage on top of
    # this -- the (0.5**age)*n weighting already down-weights a thin or old season on its own,
    # and stacking both was tried and confirmed (against the same real Winston/Mariota check) to
    # over-dampen a real, meaningful track record back toward a falsely-neutral number. _shrink()
    # is for a SINGLE season's raw estimate standing alone (e.g. detecting an in-season backup
    # change from a couple of games); this is already an aggregate of several real seasons.
    cols = ["play_type", "epa", "posteam", "passer_player_name"]
    parts = []
    for years_back, weight in [(0, 1.0), (1, 0.5), (2, 0.25), (3, 0.125)]:
        yr = season - years_back
        path = current_season_pbp_path if years_back == 0 else fetch_pbp(yr)
        if not path:
            continue
        pbp = pd.read_parquet(path, columns=cols)
        min_att = 10 if years_back == 0 else 30  # a partial current season needs a lower bar
        raw = _passer_epa_vs_league(pbp, qb_name, min_attempts=min_att)
        if raw:
            parts.append((raw[0], raw[1], weight))
    if not parts:
        return None
    total_w = sum(n * w for _, n, w in parts)
    return max(-0.25, min(0.25, sum(v * n * w for v, n, w in parts) / total_w))


def run_ratings(season, week, prior_season_pbp_path):
    pbp_path = fetch_pbp(season)
    tg = build_team_games(pbp_path, season)
    # Always the full real 32-team league list, from the schedule -- NOT tg.team.unique(),
    # which only reflects teams with actual current-season pbp so far. Mid-week (some games
    # played, most not), that used to silently shrink to just the handful of teams who'd
    # already played, which then KeyError'd the moment fit_split() tried to index a PRIOR-
    # season team (e.g. ARI, who just hadn't played yet this week) against that too-small map.
    teams = _real_teams_for_season(season)
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
    epa_sums, totals = [], []
    for _, r in completed.iterrows():
        try:
            ho = tg_idx.loc[(r.game_id, r.home_team), "off_epa"]
            ao = tg_idx.loc[(r.game_id, r.away_team), "off_epa"]
        except KeyError:
            continue
        epa_diffs.append(ho - ao); margins.append(float(r.result))
        if pd.notna(r.total):
            epa_sums.append(ho + ao); totals.append(float(r.total))
    if len(epa_diffs) >= 5:
        pts_per_epa = np.linalg.lstsq(np.column_stack([epa_diffs, np.ones(len(epa_diffs))]), margins, rcond=None)[0][0]
    else:
        pts_per_epa = 44.0  # fallback for very early season, before enough games exist
    # Same real-regression approach as pts_per_epa above, just fit against each game's real
    # combined score instead of the real margin -- a team's own projected total is then
    # (model_total + model_margin)/2 (see games_out below), not a separately-guessed number.
    # Backtested (2024-2025, real walk-forward, McNemar-tested) before this was ever written
    # into the pipeline: O/U record 51.5%, not significantly different from a coin flip and
    # below real -110 breakeven -- same honest, no-proven-edge character as the spread model
    # itself, not something stronger. Shipped anyway on that basis (a real, backtested, no-
    # fabricated-edge projection, same standard the spread already meets), not as a claimed win.
    if len(epa_sums) >= 5:
        total_fit = np.linalg.lstsq(np.column_stack([epa_sums, np.ones(len(epa_sums))]), totals, rcond=None)[0]
        total_slope, total_intercept = float(total_fit[0]), float(total_fit[1])
    else:
        total_slope, total_intercept = 22.0, 44.0  # crude early-season fallback, same spirit as pts_per_epa's

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
        "total_slope": total_slope, "total_intercept": total_intercept,
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


# ---------- Team totals: real market lines for each team's own projected points ----------
# Unlike spreads (one bulk call for the whole week), team_totals only exists on the per-event
# odds endpoint -- confirmed live against the real account before this was written (HTTP 200,
# real bookmaker data, no plan upgrade needed). That means one real API call per game, not one
# call for the whole week -- real, deliberate extra cost (roughly len(games) credits per run),
# affordable on a 20K/month plan but worth knowing about if this pipeline starts running much
# more often. Returns {} entirely (never raises) on any failure, since this is a real, additive
# feature -- a book not covering it yet, or a transient API hiccup, shouldn't take down the
# whole weekly update over a market that's separate from the spread the rest of the pipeline
# depends on.
def fetch_team_totals_for_week(api_key, week_games):
    import urllib.request
    try:
        events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/?apiKey={api_key}"
        with urllib.request.urlopen(events_url, timeout=20) as resp:
            events = json.loads(resp.read())
    except Exception as e:
        print(f"  (team_totals: couldn't fetch real event list, skipping entirely -- {e})")
        return {}

    out = {}
    for _, r in week_games.iterrows():
        home_full, away_full = REV_MAP[r.home_team], REV_MAP[r.away_team]
        event = next((e for e in events if e.get("home_team")==home_full and e.get("away_team")==away_full), None)
        if not event:
            continue
        gid = f"{r.away_team.lower()}-{r.home_team.lower()}"
        url = (f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event['id']}/odds"
               f"?apiKey={api_key}&regions=us&markets=team_totals&oddsFormat=american")
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  team_totals {gid}: couldn't fetch -- {e}")
            continue
        home_pts, away_pts = [], []
        for bm in data.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk["key"] != "team_totals":
                    continue
                for oc in mk["outcomes"]:
                    # team_totals outcomes are named "Over"/"Under" with a separate "description"
                    # field naming which team's total the line belongs to.
                    if oc.get("name") != "Over":
                        continue
                    desc = oc.get("description")
                    if desc == home_full and oc.get("point") is not None:
                        home_pts.append(oc["point"])
                    elif desc == away_full and oc.get("point") is not None:
                        away_pts.append(oc["point"])
        if home_pts or away_pts:
            out[gid] = {
                "market_home_total": round(sum(home_pts)/len(home_pts), 2) if home_pts else None,
                "market_away_total": round(sum(away_pts)/len(away_pts), 2) if away_pts else None,
                "n_books": max(len(home_pts), len(away_pts)),
            }
    return out


# ---------- Player prop value: real multi-book devig + consensus, not a from-scratch model ----------
# Deliberately does NOT try to out-predict the market (that's a much higher, unproven bar -- see
# the spread/total backtests). Pulls every real book's price for the same real player prop,
# removes each book's own vig (devig), averages into a real consensus "what does the market as a
# whole think," then flags whichever single book's price is out of line with that consensus --
# real line-shopping/soft-book detection, the same technique tools like Unabated's Props
# Simulator and Market-Based Projections are built around (confirmed via their own public docs
# before this was written), not a novel or unvalidated idea.
#
# Real, verified cost before this was ever written as a permanent feature: 5 credits per event
# for this exact market set (confirmed live against the real account), so a full ~16-game week
# costs roughly 80 credits -- trivial against a 20K/month plan even run every single pipeline run.
PROP_MARKETS = "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_pass_tds,player_pass_completions,player_rush_attempts,player_anytime_td"
# Real retroactive backtest against weeks 1-4 of this season (98 real flagged edges at the old
# 3pp cutoff, 85 graded): 45.9% win rate vs. a 47.5% breakeven -- no real evidence this signal
# clears the vig at 3pp, and the largest edge found all season was under 5pp, so there isn't
# enough real spread in the data yet to prove a precise optimal cutoff either. This bump to 4pp
# is a modest, honestly-labeled interim tightening (fewer, slightly cleaner-looking edges), NOT
# a data-proven fix -- a real threshold-tuned curve needs the full per-edge dataset persisted,
# not just the top-20 sample this backtest printed.
_MIN_EDGE_PP = 0.04

def _american_to_prob(odds):
    odds = float(odds)
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)

def build_prop_value_report(api_key, week_games):
    import urllib.request
    try:
        events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/?apiKey={api_key}"
        with urllib.request.urlopen(events_url, timeout=20) as resp:
            events = json.loads(resp.read())
    except Exception as e:
        print(f"  (prop_value: couldn't fetch real event list, skipping entirely -- {e})")
        return []

    all_edges = []
    for _, r in week_games.iterrows():
        home_full, away_full = REV_MAP[r.home_team], REV_MAP[r.away_team]
        event = next((e for e in events if e.get("home_team")==home_full and e.get("away_team")==away_full), None)
        if not event:
            continue
        gid = f"{r.away_team.lower()}-{r.home_team.lower()}"
        url = (f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event['id']}/odds"
               f"?apiKey={api_key}&regions=us&markets={PROP_MARKETS}&oddsFormat=american")
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  prop_value {gid}: couldn't fetch -- {e}")
            continue

        # (market, player, line) -> {book: {Over: price, Under: price}}
        props = {}
        for bm in data.get("bookmakers", []):
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
                continue  # can't form a real consensus off a single book
            devigged = {}
            for book, sides in books.items():
                p_over = _american_to_prob(sides["Over"])
                p_under = _american_to_prob(sides["Under"])
                total = p_over + p_under
                devigged[book] = {"over": p_over/total, "under": p_under/total,
                                   "over_odds": sides["Over"], "under_odds": sides["Under"]}
            consensus_over = sum(d["over"] for d in devigged.values()) / len(devigged)
            for book, d in devigged.items():
                edge_over = consensus_over - d["over"]
                edge_under = (1 - consensus_over) - d["under"]
                if edge_over > _MIN_EDGE_PP:
                    all_edges.append({"game_id": gid, "market": market_key, "player": player, "line": point,
                                       "side": "Over", "book": book, "odds": d["over_odds"],
                                       "edge_pp": round(edge_over*100, 1), "consensus_pct": round(consensus_over*100, 1),
                                       "book_implied_pct": round(d["over"]*100, 1), "n_books": len(books)})
                if edge_under > _MIN_EDGE_PP:
                    all_edges.append({"game_id": gid, "market": market_key, "player": player, "line": point,
                                       "side": "Under", "book": book, "odds": d["under_odds"],
                                       "edge_pp": round(edge_under*100, 1), "consensus_pct": round((1-consensus_over)*100, 1),
                                       "book_implied_pct": round(d["under"]*100, 1), "n_books": len(books)})

    all_edges.sort(key=lambda e: -e["edge_pp"])
    return all_edges


# ---------- STEP 4: previous week's closing lines + results ----------
def build_closing_results(games_prev_week):
    out = {}
    for _, r in games_prev_week.iterrows():
        if pd.isna(r.result) or r.result == "":
            continue
        gid = f"{r.away_team.lower()}-{r.home_team.lower()}"
        entry = {"close_home": float(r.spread_line), "away_score": int(r.away_score), "home_score": int(r.home_score)}
        # Real closing game total (nflverse's own total_line) -- same real-closing-number
        # standard the spread grading already holds itself to, now available for grading the
        # total model the exact same honest way, not against a live "market" snapshot that can
        # go stale/null once a game's kicked off.
        if pd.notna(r.total_line):
            entry["close_total"] = float(r.total_line)
        out[gid] = entry
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
    # Always the full real 32-team league list, from the schedule -- NOT tg.team.unique(),
    # which only reflects teams with actual current-season pbp so far. Mid-week (some games
    # played, most not), that used to silently shrink to just the handful of teams who'd
    # already played, which then KeyError'd the moment fit_split() tried to index a PRIOR-
    # season team (e.g. ARI, who just hadn't played yet this week) against that too-small map.
    teams = _real_teams_for_season(season)
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
            off_pts = round(off[t]*pts_per_epa, 1)
            # deft[t] is fit so that a HIGHER value means "suppresses the opponent's offense
            # more" (see fit_split: predicted off_epa = off[team] - deft[opp]) -- i.e. a higher
            # deft is a BETTER defense. def_pts is meant to read as "EPA/play allowed relative
            # to average, lower is better" (that's what every label/color/chart downstream of
            # this already assumes), so it has to be the negation of deft, not deft itself --
            # storing it un-negated was the actual bug behind a good defense (e.g. a real
            # top-5 defense by raw EPA allowed) scoring as one of the worst in the league.
            def_pts = round(-deft[t]*pts_per_epa, 1)
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
                     qb_history, team_players, fantasy_projections, underperformance_report=None,
                     team_full_roster=None, team_situational=None, team_diagnostics=None,
                     prop_value=None):
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

    # prop_value/current -- real multi-book devig/consensus edges, this week only (full
    # overwrite is correct here, not an accumulate-across-weeks collection like closing_results --
    # last week's prop lines are dead once those games are over).
    if prop_value is not None:
        db.collection("prop_value").document("current").set({
            "season": season, "week": week, "edges": prop_value,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        written.append(f"prop_value/current ({len(prop_value)} edges)")

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
        doc = {
            "qb": qb_leaderboard, "wr": wr_leaderboard, "rb": rb_leaderboard,
            "qb_history": qb_history, "team_players": team_players,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        if team_full_roster is not None: doc["team_full_roster"] = team_full_roster
        if team_situational is not None: doc["team_situational"] = team_situational
        if team_diagnostics is not None: doc["team_diagnostics"] = team_diagnostics
        db.collection("leaderboards").document("current").set(doc)
        written.append("leaderboards/current")
    elif team_diagnostics is not None:
        # team_diagnostics only needs real games.csv (no PBP on disk required), so it's still
        # worth writing even on a run where the PBP-dependent fields above were skipped -- a
        # merge, not a full overwrite, so it can't clobber real qb/wr/rb/team_players data that
        # a prior run already wrote.
        db.collection("leaderboards").document("current").set(
            {"team_diagnostics": team_diagnostics, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        written.append("leaderboards/current (team_diagnostics only, merged)")

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
    # Totals graded the same real-closing-line standard as spread (close_total, straight from
    # nflverse -- see build_closing_results). Team totals CAN'T be held to that same standard --
    # there's no real historical/closing per-team-total data source (checked before building the
    # totals feature at all) -- so team-total grading uses the last real market_home_total/
    # market_away_total this pipeline itself stored for that game before kickoff, which is real
    # and honest but not a true close the way close_home/close_total are. Both start accumulating
    # from whenever the totals feature shipped -- older weeks' games docs don't have model_total/
    # market_home_total at all and are skipped, not backfilled with a fabricated number.
    total_graded, total_by_week = [], {}
    team_total_graded = []
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
            wk_key = str(wk)  # Firestore map keys must be strings -- "week" stays a real int everywhere else
            by_week.setdefault(wk_key, {"wins": 0, "losses": 0, "pushes": 0})
            by_week[wk_key][{"win": "wins", "loss": "losses", "push": "pushes"}[grade]] += 1

            # ---- Total (game combined score) -- real closing number, same rigor as spread ----
            close_total = cr.get("close_total")
            model_total = g.get("model_total")
            if close_total is not None and model_total is not None:
                actual_total = cr["home_score"] + cr["away_score"]
                t_edge = model_total - close_total
                model_pick = "over" if t_edge > 0 else "under"
                t_diff = actual_total - close_total
                if abs(t_diff) < 1e-9:
                    t_grade = "push"
                else:
                    went_over = t_diff > 0
                    t_grade = "win" if went_over == (model_pick == "over") else "loss"
                total_graded.append({"week": wk, "game_id": g["id"], "pick": model_pick,
                                      "edge": round(abs(t_edge), 2), "grade": t_grade})
                total_by_week.setdefault(wk_key, {"wins": 0, "losses": 0, "pushes": 0})
                total_by_week[wk_key][{"win": "wins", "loss": "losses", "push": "pushes"}[t_grade]] += 1

            # ---- Each team's own total -- graded vs. our own last stored market snapshot ----
            for side, team in (("home", g["home"]), ("away", g["away"])):
                mkt = g.get(f"market_{side}_total")
                mdl = g.get(f"model_{side}_total")
                if mkt is None or mdl is None:
                    continue
                actual = cr["home_score"] if side == "home" else cr["away_score"]
                tt_edge = mdl - mkt
                pick = "over" if tt_edge > 0 else "under"
                tt_diff = actual - mkt
                if abs(tt_diff) < 1e-9:
                    tt_grade = "push"
                else:
                    tt_grade = "win" if (tt_diff > 0) == (pick == "over") else "loss"
                team_total_graded.append({"week": wk, "game_id": g["id"], "team": team, "pick": pick,
                                           "edge": round(abs(tt_edge), 2), "grade": tt_grade})

    wins = sum(1 for r in graded if r["grade"] == "win")
    losses = sum(1 for r in graded if r["grade"] == "loss")
    pushes = sum(1 for r in graded if r["grade"] == "push")
    win_pct = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else None

    t_wins = sum(1 for r in total_graded if r["grade"] == "win")
    t_losses = sum(1 for r in total_graded if r["grade"] == "loss")
    t_pushes = sum(1 for r in total_graded if r["grade"] == "push")
    t_win_pct = round(t_wins / (t_wins + t_losses) * 100, 1) if (t_wins + t_losses) > 0 else None

    tt_wins = sum(1 for r in team_total_graded if r["grade"] == "win")
    tt_losses = sum(1 for r in team_total_graded if r["grade"] == "loss")
    tt_pushes = sum(1 for r in team_total_graded if r["grade"] == "push")
    tt_win_pct = round(tt_wins / (tt_wins + tt_losses) * 100, 1) if (tt_wins + tt_losses) > 0 else None

    return {
        "season": season, "wins": wins, "losses": losses, "pushes": pushes, "win_pct": win_pct,
        "total_graded": len(graded), "by_week": by_week, "games": graded,
        "totals": {"wins": t_wins, "losses": t_losses, "pushes": t_pushes, "win_pct": t_win_pct,
                   "total_graded": len(total_graded), "by_week": total_by_week, "games": total_graded},
        "team_totals": {"wins": tt_wins, "losses": tt_losses, "pushes": tt_pushes, "win_pct": tt_win_pct,
                         "total_graded": len(team_total_graded), "games": team_total_graded,
                         "note": "Graded vs. the last market number this pipeline itself captured before kickoff, not a true historical close (no real closing per-team-total data source exists) -- honest, but a slightly different standard than the spread/total records above."},
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

def build_team_top_players(pbp_paths_and_seasons, min_carries=5, min_targets=3):
    """Per-team snapshot: current-ish starting QB, top rusher by rush EPA/play, top receiver by
    YAC-over-expected, and team pass-block context (sack rate, QB-hit rate, both as a fraction
    of real dropbacks) -- ALL of it restricted to the most recent real season present in
    pbp_paths_and_seasons, not blended across multiple seasons. That used to only be true for
    the QB pick; rusher/receiver/sack-rate pooled every season in the window together, which
    meant a full prior season's real production could keep crediting a player to a team he'd
    since been traded away from, since a whole season's sample usually beats a handful of real
    games for whoever actually has the job now (caught for real: a just-traded RB still shown
    as his old team's top receiver after a big Week 1 for his new one). min_carries/min_targets
    are deliberately modest (not scaled to a full season) so a real Week 1 alone already
    produces a real, current answer instead of coming back empty until enough weeks pile up."""
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
        tp_all = allp[allp.posteam == team]
        latest_season = tp_all.season.max()
        tp = tp_all[tp_all.season == latest_season]  # current season only, no prior-season blend

        qb_pool = tp[(tp.play_type == "pass") & tp.passer.notna()]
        qb = qb_pool.passer.value_counts().idxmax() if len(qb_pool) else None

        # Real QB-change flag: compares this season's real starter against the prior real
        # season's -- e.g. Seattle's team RATING doesn't know Darnold->Lock happened (it's a
        # team-level stat with no player identity at all, and it's still mostly running on last
        # season's carryover prior this early), so this is a cheap, honest way to flag "the
        # rating above may be stale for personnel reasons" using data already loaded here,
        # rather than pretending the model itself accounts for it.
        prior_seasons = tp_all[tp_all.season < latest_season].season
        prior_qb = None
        if len(prior_seasons):
            prior_season = prior_seasons.max()
            prior_pool = tp_all[(tp_all.season == prior_season) & (tp_all.play_type == "pass") & tp_all.passer.notna()]
            prior_qb = prior_pool.passer.value_counts().idxmax() if len(prior_pool) else None
        qb_change = bool(qb and prior_qb and qb != prior_qb)
        # Every real passer this team has used this season -- excluded from "top rusher"/"top
        # receiver" below. Without this, a QB's scramble EPA (small sample, often garbage-time/
        # broken-play) can look like an elite rushing season and wrongly surface as the team's
        # top rusher (caught this for real: J.Flacco was coming back as CIN's "top rusher" off
        # a handful of scrambles before this filter).
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
                     "sack_rate": sack_rate, "hit_rate": hit_rate,
                     "qb_change": qb_change, "prior_qb": prior_qb}
    return out


def build_team_full_roster(pbp_paths_and_seasons, min_pass_att=5, min_carries=3, min_targets=2):
    """Every real player at QB/RB/WR-TE who's seen meaningful current-season snaps for each
    team, not just the single top name per role that build_team_top_players surfaces -- the
    full context behind "the rating above comes from every play this team has run," not just a
    sample of it. Same real, current-season-only restriction as build_team_top_players (no
    blending in a departed player's prior-season production), same reasoning for modest
    thresholds (a real Week 1 alone should already produce a real, non-empty answer).

    Replaces a hardcoded, one-time JS constant of the same name that was never regenerated by
    this pipeline at all -- caught for real: it still had Kyler Murray as Arizona's QB, a real
    2025 fact this fictional season's real roster shuffle already made false.
    """
    cols = ["posteam", "passer", "rusher", "receiver", "epa", "cpoe", "play_type", "season_type",
            "complete_pass", "passing_yards", "rushing_yards", "receiving_yards",
            "pass_touchdown", "rush_touchdown", "yards_after_catch", "xyac_mean_yardage"]
    frames = []
    for path, season in pbp_paths_and_seasons:
        p = pd.read_parquet(path, columns=cols)
        p = p[p.season_type == "REG"].copy()
        p["season"] = season
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)

    out = {}
    for team in sorted(allp.posteam.dropna().unique()):
        tp_all = allp[allp.posteam == team]
        latest_season = tp_all.season.max()
        tp = tp_all[tp_all.season == latest_season]  # current season only, matches build_team_top_players

        qb_pool = tp[(tp.play_type == "pass") & tp.passer.notna()]
        qb_stats = qb_pool.groupby("passer").agg(
            cpoe=("cpoe", "mean"), epa=("epa", "mean"), n=("epa", "size"),
            comp=("complete_pass", "sum"), yards=("passing_yards", "sum"), tds=("pass_touchdown", "sum"),
        )
        qb_stats = qb_stats[qb_stats.n >= min_pass_att].sort_values("n", ascending=False)
        qbs = [{"name": name, "cpoe": round(float(r.cpoe), 2) if pd.notna(r.cpoe) else None,
                "epa": round(float(r.epa), 3), "n": int(r.n),
                "comp_pct": round(float(r.comp) / r.n * 100, 1), "yards": int(r.yards), "tds": int(r.tds)}
               for name, r in qb_stats.iterrows()]

        team_passers = set(tp[tp.play_type == "pass"].passer.dropna().unique())

        rush_pool = tp[(tp.play_type == "run") & tp.rusher.notna() & ~tp.rusher.isin(team_passers)]
        rush_stats = rush_pool.groupby("rusher").agg(
            epa=("epa", "mean"), n=("epa", "size"), yards=("rushing_yards", "sum"), tds=("rush_touchdown", "sum"))
        rush_stats = rush_stats[rush_stats.n >= min_carries].sort_values("n", ascending=False)
        rushers = [{"name": name, "epa": round(float(r.epa), 3), "n": int(r.n),
                    "yards": int(r.yards), "tds": int(r.tds)}
                   for name, r in rush_stats.iterrows()]

        rec_pool = tp[(tp.play_type == "pass") & (tp.complete_pass == 1) & tp.receiver.notna()
                      & ~tp.receiver.isin(team_passers)].copy()
        rec_pool["yac_oe"] = rec_pool.yards_after_catch - rec_pool.xyac_mean_yardage
        rec_stats = rec_pool.groupby("receiver").agg(
            yac_oe=("yac_oe", "mean"), n=("yac_oe", "size"),
            yards=("receiving_yards", "sum"), tds=("pass_touchdown", "sum"))
        rec_stats = rec_stats[rec_stats.n >= min_targets].sort_values("n", ascending=False)
        receivers = [{"name": name, "yac_oe": round(float(r.yac_oe), 2), "n": int(r.n),
                      "yards": int(r.yards), "tds": int(r.tds)}
                     for name, r in rec_stats.iterrows()]

        out[team] = {"qbs": qbs, "rushers": rushers, "receivers": receivers}
    return out


def build_team_situational_stats(pbp_paths_and_seasons):
    """Real team-level box-score/situational stats the model never sees at all -- opponent-
    adjusted EPA captures overall play efficiency, not third-down conversion, red-zone finishing,
    turnover margin, or penalties specifically. Current-season-only, same restriction as the
    player functions above. Red zone efficiency is real drive-level (drive_inside20 /
    fixed_drive_result), not a play-level approximation. Fumble recovery rate is specifically
    isolated because it's the most well-established "luck" component of turnover margin in real
    football research -- recovering a live ball is close to a 50/50 proposition league-wide, so a
    team recovering way more or fewer than half of its own fumbles is a real signal that turnover
    margin (offense) is running hot or cold, not that they're unusually good/bad at ball security.
    """
    cols = ["posteam", "defteam", "season_type", "down", "third_down_converted", "third_down_failed",
            "interception", "fumble_lost", "fumble", "fumble_recovery_1_team", "sack",
            "penalty_team", "penalty_yards", "game_id", "fixed_drive", "fixed_drive_result", "drive_inside20"]
    frames = []
    for path, season in pbp_paths_and_seasons:
        p = pd.read_parquet(path, columns=cols)
        p = p[p.season_type == "REG"].copy()
        p["season"] = season
        frames.append(p)
    allp = pd.concat(frames, ignore_index=True)

    teams = sorted(set(allp.posteam.dropna().unique()) | set(allp.defteam.dropna().unique()))
    out = {}
    for team in teams:
        involved = allp[(allp.posteam == team) | (allp.defteam == team)]
        if len(involved) == 0:
            continue
        latest_season = involved.season.max()
        p = allp[allp.season == latest_season]
        off = p[p.posteam == team]
        deft = p[p.defteam == team]

        third = off[off.down == 3]
        conv, failed = int(third.third_down_converted.sum()), int(third.third_down_failed.sum())
        third_down_pct = round(conv / (conv + failed) * 100, 1) if (conv + failed) > 0 else None

        giveaways = int(off.interception.sum() + off.fumble_lost.sum())
        takeaways = int(deft.interception.sum() + deft.fumble_lost.sum())

        off_fumbles = off[off.fumble == 1]
        recovered_by_self = int((off_fumbles.fumble_recovery_1_team == team).sum())
        fumble_recovery_pct = round(recovered_by_self / len(off_fumbles) * 100, 1) if len(off_fumbles) > 0 else None

        off_drives = off.drop_duplicates(["game_id", "fixed_drive"])
        rz_drives = off_drives[off_drives.drive_inside20 == 1]
        rz_td = int((rz_drives.fixed_drive_result == "Touchdown").sum())
        red_zone_td_pct = round(rz_td / len(rz_drives) * 100, 1) if len(rz_drives) > 0 else None

        pen = p[p.penalty_team == team]

        out[team] = {
            "third_down_pct": third_down_pct,
            "giveaways": giveaways, "takeaways": takeaways, "turnover_margin": takeaways - giveaways,
            "fumble_recovery_pct": fumble_recovery_pct, "own_fumbles": len(off_fumbles),
            "sacks_taken": int(off.sack.sum()), "sacks_recorded": int(deft.sack.sum()),
            "red_zone_td_pct": red_zone_td_pct, "red_zone_trips": int(len(rz_drives)),
            "penalties": int(len(pen)), "penalty_yards": int(pen.penalty_yards.sum()),
        }
    return out


def build_team_diagnostics(season, week):
    """Real record vs. real point-differential (Pythagorean) expectation, close-game record, and
    real EPA rank -- the honest "is this real or a fluke" answer for a team's record, using
    established, well-known real football-analytics signals instead of a vibe. A team winning
    well ahead of its point differential, or with an unusual share of one-score games, is a real,
    documented pattern that tends to regress -- not a fabricated threshold, standard sabermetric-
    style reasoning applied to real games.csv data (2.37 exponent is the commonly cited real NFL
    Pythagorean exponent, from Football Outsiders' own published research, not invented here).
    """
    games = pd.read_csv(fetch_games_csv())
    g = games[(games.season == season) & (games.game_type == "REG") & (games.week < week) & games.result.notna()]
    teams = sorted(set(g.home_team) | set(g.away_team))

    out = {}
    for team in teams:
        wins = losses = ties = 0
        pf = pa = 0
        close_wins = close_losses = 0
        for _, r in pd.concat([
            g[g.home_team == team].assign(pf=lambda d: d.home_score, pa=lambda d: d.away_score),
            g[g.away_team == team].assign(pf=lambda d: d.away_score, pa=lambda d: d.home_score),
        ]).iterrows():
            margin = r.pf - r.pa
            pf += r.pf; pa += r.pa
            if margin > 0: wins += 1
            elif margin < 0: losses += 1
            else: ties += 1
            if abs(margin) <= 8:
                if margin > 0: close_wins += 1
                elif margin < 0: close_losses += 1

        games_played = wins + losses + ties
        if games_played == 0:
            continue
        actual_win_pct = (wins + 0.5 * ties) / games_played
        pyth_win_pct = (pf ** 2.37) / (pf ** 2.37 + pa ** 2.37) if (pf > 0 or pa > 0) else None
        pyth_wins = pyth_win_pct * games_played if pyth_win_pct is not None else None

        out[team] = {
            "wins": wins, "losses": losses, "ties": ties, "games_played": games_played,
            "pf": int(pf), "pa": int(pa), "point_diff": int(pf - pa),
            "actual_win_pct": round(actual_win_pct * 100, 1),
            "pyth_win_pct": round(pyth_win_pct * 100, 1) if pyth_win_pct is not None else None,
            # positive = winning MORE than real point differential says they should -- the real,
            # standard "riding luck" signal; negative = the opposite ("better than their record").
            "wins_above_pythagorean": round(wins - pyth_wins, 2) if pyth_wins is not None else None,
            "close_record": f"{close_wins}-{close_losses}", "close_games": close_wins + close_losses,
        }
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

    prior_season_pbp_path_val = ("/home/claude/odds_pull/pbp_2024.parquet"
                                  if SEASON == 2025 else fetch_pbp(SEASON - 1))
    ratings = run_ratings(SEASON, WEEK, prior_season_pbp_path=prior_season_pbp_path_val)

    odds_data = pull_week_odds(MODE, API_KEY, HIST_DATE)
    books = build_books_for_week(odds_data, ratings["games_this_week"])
    team_totals_data = fetch_team_totals_for_week(API_KEY, ratings["games_this_week"])
    print(f"\n--- TEAM TOTALS: real market lines fetched for {len(team_totals_data)} of {len(ratings['games_this_week'])} games ---")

    prop_value_out = build_prop_value_report(API_KEY, ratings["games_this_week"])
    print(f"\n--- PROP VALUE: {len(prop_value_out)} real flagged edges across this week's games ---")
    print(json.dumps(prop_value_out[:20], indent=1), "\n...(top 20 shown)" if len(prop_value_out) > 20 else "")
    # Grade previous week's games (normal weekly cadence) PLUS any game in the CURRENT
    # week's slate that has already gone final -- e.g. re-running mid-week after a
    # Thursday/Sunday-night opener finishes, without waiting for the whole week to end.
    closing = build_closing_results(pd.concat([ratings["games_prev_week"], ratings["games_this_week"]]))
    weather = pull_weather_for_week(ratings["games_this_week"])

    # ---- RATINGS array ----
    rows = []
    for t in ratings["teams"]:
        off_pts = round(ratings["off"][t] * ratings["pts_per_epa"], 1)
        # See the matching comment in run_rating_history() -- def_pts has to be the negation
        # of the raw deft coefficient (a higher deft = a better defense) so that it reads as
        # "EPA/play allowed, lower is better" the way every label/color downstream expects.
        # NOTE: the game-by-game model spread below uses ratings["deft"] directly (un-negated,
        # correctly) -- that math was never affected by this, only this display/ranking value.
        def_pts = round(-ratings["deft"][t] * ratings["pts_per_epa"], 1)
        overall = round(off_pts - def_pts, 1)
        off_prev_pts = ratings["off_prev"][t] * ratings["pts_per_epa"]
        def_prev_pts = -ratings["deft_prev"][t] * ratings["pts_per_epa"]
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
    # Auto (real depth-chart + injury cross-check, refreshed ~2x/real-day by its own separate
    # workflow -- see qb_status_check.py) applies by default; a real, active manual override
    # for that same team wins on top of it (dict union -- manual_overrides' keys take priority)
    # for the genuine edge cases automation can't call (breaking news with no real depth-chart/
    # injury-report signal yet).
    qb_auto = fetch_qb_depth_chart_status_dict(SEASON, fetch_pbp(SEASON))
    qb_manual = fetch_qb_status_overrides(SEASON, fetch_pbp(SEASON))
    qb_overrides = {**qb_auto, **qb_manual}
    if qb_auto:
        print(f"  QB auto-detect: {len(qb_auto)} team(s) flagged ({', '.join(qb_auto)})" +
              (f" -- {len(qb_manual)} overridden by manual entries" if qb_manual else ""))
    games_out = []
    for _, r in ratings["games_this_week"].iterrows():
        h, a = r.home_team, r.away_team
        if h not in ratings["off"].index or a not in ratings["off"].index:
            continue
        # A per-GAME adjustment only -- deliberately NOT written back into ratings["off"], so it
        # doesn't leak into off_pts/overall_pts or next week's walk-forward fit. Once the new
        # starter has real snaps of their own, the rating picks them up on its own and this
        # override should be turned off (stale entries just do nothing once real data catches up
        # and someone remembers to flip `active` back off -- worth checking occasionally).
        home_off = ratings["off"][h] + qb_overrides.get(h, 0.0)
        away_off = ratings["off"][a] + qb_overrides.get(a, 0.0)
        home_net = home_off - ratings["deft"][a]
        away_net = away_off - ratings["deft"][h]
        model_home_favored = (home_net - away_net + ratings["hfa"]) * ratings["pts_per_epa"]
        gid = f"{a.lower()}-{h.lower()}"
        book_entry = books.get(gid)
        market_home_favored = -float(book_entry["books"][0]["home_pt"]) if book_entry else None

        # Team totals: the combined-total regression (total_slope/intercept, real-fit above,
        # same method as pts_per_epa) gives the projected game total; each team's own total then
        # just splits that against the already-projected margin (model_home_favored, which is
        # literally the projected home-minus-away margin in normal sign before the -1 flip below
        # converts it to this file's stored odds-api-style convention). No separate model needed
        # for the split -- see the backtest note above the total_slope/intercept fit for the real,
        # honest accuracy read on this (comparable to the spread model's own -- no proven edge,
        # shipped on the same "real, backtested, no fabricated edge" basis the spread already is).
        model_total = (home_net + away_net) * ratings["total_slope"] + ratings["total_intercept"]
        model_home_total = round((model_total + model_home_favored) / 2, 2)
        model_away_total = round((model_total - model_home_favored) / 2, 2)
        tt_entry = team_totals_data.get(gid)

        games_out.append({
            "id": gid, "away": a, "home": h, "week": WEEK, "season": SEASON,
            # gameday/gametime come straight from the real nflverse schedule (games.csv) --
            # gametime is already ET, same as every other time shown in this dashboard.
            "gameday": str(r.gameday) if pd.notna(r.gameday) else None,
            "gametime": str(r.gametime) if pd.notna(r.gametime) else None,
            "model": round(-model_home_favored, 2),
            "market": round(-market_home_favored, 2) if market_home_favored is not None else None,
            "model_total": round(model_total, 2),
            "model_home_total": model_home_total,
            "model_away_total": model_away_total,
            "market_home_total": tt_entry["market_home_total"] if tt_entry else None,
            "market_away_total": tt_entry["market_away_total"] if tt_entry else None,
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
            # attempts carried through per week (not just the season-long 100+ gate above) so a
            # real but tiny-sample week -- a single mop-up-duty relief snap, e.g. -- can be told
            # apart from a real, meaningful game when something downstream (like the "biggest
            # single-game CPOE" notable fact) decides whether to actually feature it.
            qb_history_out[name] = [{"week": int(w), "cpoe": round(float(c), 2), "attempts": int(a)}
                                     for w, c, a in zip(grp.week, grp.cpoe, grp.attempts)]
        print(f"\n--- QB_HISTORY (real, {most_recent_season} only -- paste into const QB_HISTORY = { '{' } ... { '}' }) ---")
        print(json.dumps(qb_history_out, indent=1))

        team_players_out = build_team_top_players(pbp_local)
        print(f"\n--- TEAM_PLAYERS (real, {'+'.join(str(y) for _,y in pbp_local)}, 'qb' reflects {most_recent_season}'s most-used passer per team -- paste into const TEAM_PLAYERS = { '{' } ... { '}' }) ---")
        print(json.dumps(team_players_out, indent=1))

        # Replaces the old hardcoded, never-refreshed TEAM_FULL_ROSTER JS constant (still had
        # Kyler Murray as Arizona's real QB -- a fact this season's real roster shuffle already
        # made false). Real, current-season-only, every qualifying player at each position, not
        # just the single top name TEAM_PLAYERS surfaces.
        team_full_roster_out = build_team_full_roster(pbp_local)
        print(f"\n--- TEAM_FULL_ROSTER (real, {most_recent_season} only -- paste into const TEAM_FULL_ROSTER = { '{' } ... { '}' }) ---")
        print(json.dumps(team_full_roster_out, indent=1))

        team_situational_out = build_team_situational_stats(pbp_local)
        print(f"\n--- TEAM_SITUATIONAL (real, {most_recent_season} only -- 3rd down/red zone/turnovers/penalties -- paste into const TEAM_SITUATIONAL = { '{' } ... { '}' }) ---")
        print(json.dumps(team_situational_out, indent=1))
    else:
        print("\n--- QB_LEADERBOARD / WR_LEADERBOARD / RB_LEADERBOARD / QB_HISTORY / TEAM_PLAYERS / TEAM_FULL_ROSTER / TEAM_SITUATIONAL ---")
        print("No real pbp available locally (checked " + CACHE_DIR + "/pbp_*.parquet) -- skipped. "
              "These need at least one real completed season's play-by-play on disk.")
        qb_lb_out = wr_lb_out = rb_lb_out = qb_history_out = team_players_out = None
        team_full_roster_out = team_situational_out = None

    # Real record vs. real point-differential expectation, close-game record -- see
    # build_team_diagnostics() docstring. Doesn't need real PBP on disk (games.csv only), so this
    # runs unconditionally, unlike the player-level stuff above.
    team_diagnostics_out = build_team_diagnostics(SEASON, WEEK)
    print(f"\n--- TEAM_DIAGNOSTICS (real, through week {WEEK-1} -- paste into const TEAM_DIAGNOSTICS = { '{' } ... { '}' }) ---")
    print(json.dumps(team_diagnostics_out, indent=1))

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
            team_full_roster=team_full_roster_out, team_situational=team_situational_out,
            team_diagnostics=team_diagnostics_out, prop_value=prop_value_out,
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


