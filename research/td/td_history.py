"""Buy historical anytime-TD odds from The Odds API (credits reset monthly; this spends what would expire).
One snapshot per game, 90 minutes before kickoff, all US books. Stops at BUDGET credits or when fewer than FLOOR remain.
Output: research/td/odds/<season>_wk<week>.json  [{game_id, event_id, kickoff, snapshot, bookmakers: [...]}]"""
import gzip, io, json, os, sys, time
from datetime import datetime, timedelta, timezone
import pandas as pd, requests

KEY, BUDGET, FLOOR = os.environ["ODDS_API_KEY"], int(os.environ.get("BUDGET", 1700)), int(os.environ.get("FLOOR", 200))
H = "https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl"
OUT = os.path.join(os.path.dirname(__file__), "odds")
os.makedirs(OUT, exist_ok=True)
NAMES = {"ARI":"Arizona Cardinals","ATL":"Atlanta Falcons","BAL":"Baltimore Ravens","BUF":"Buffalo Bills","CAR":"Carolina Panthers",
 "CHI":"Chicago Bears","CIN":"Cincinnati Bengals","CLE":"Cleveland Browns","DAL":"Dallas Cowboys","DEN":"Denver Broncos","DET":"Detroit Lions",
 "GB":"Green Bay Packers","HOU":"Houston Texans","IND":"Indianapolis Colts","JAX":"Jacksonville Jaguars","KC":"Kansas City Chiefs",
 "LV":"Las Vegas Raiders","LAC":"Los Angeles Chargers","LA":"Los Angeles Rams","MIA":"Miami Dolphins","MIN":"Minnesota Vikings",
 "NE":"New England Patriots","NO":"New Orleans Saints","NYG":"New York Giants","NYJ":"New York Jets","PHI":"Philadelphia Eagles",
 "PIT":"Pittsburgh Steelers","SF":"San Francisco 49ers","SEA":"Seattle Seahawks","TB":"Tampa Bay Buccaneers","TEN":"Tennessee Titans","WAS":"Washington Commanders"}
spent, remaining = 0, None


def get(url, params):
    global spent, remaining
    r = requests.get(url, params={**params, "apiKey": KEY}, timeout=60)
    spent += int(r.headers.get("x-requests-last", 0) or 0)
    remaining = int(float(r.headers.get("x-requests-remaining", remaining or 0) or 0))
    r.raise_for_status()
    return r.json()


g = pd.read_csv(io.BytesIO(requests.get("https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv.gz").content),
                compression="gzip")
plan = [(2026, w) for w in range(1, 9)] + [(2025, w) for w in range(18, 9, -1)]
for season, week in plan:
    path = f"{OUT}/{season}_wk{week:02d}.json"
    if os.path.exists(path):
        continue
    games = g[(g.season == season) & (g.week == week) & (g.game_type == "REG")]
    rows = []
    for kick, grp in games.groupby(["gameday", "gametime"]):
        ko = datetime.fromisoformat(f"{kick[0]}T{kick[1]}").replace(tzinfo=timezone(timedelta(hours=-4 if season == 2026 and week <= 9 else -5)))
        snap = (ko - timedelta(minutes=90)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        evs = get(f"{H}/events", {"date": snap}).get("data", [])
        for x in grp.itertuples():
            if spent >= BUDGET or (remaining is not None and remaining < FLOOR):
                break
            ev = next((e for e in evs if e["home_team"] == NAMES.get(x.home_team) and e["away_team"] == NAMES.get(x.away_team)), None)
            if not ev:
                print(f"  no event {season} wk{week} {x.away_team}@{x.home_team}"); continue
            try:
                d = get(f"{H}/events/{ev['id']}/odds", {"date": snap, "regions": "us", "markets": "player_anytime_td", "oddsFormat": "american"})
            except requests.HTTPError as e:
                print(f"  {x.game_id}: {e}"); continue
            rows.append(dict(game_id=x.game_id, event_id=ev["id"], kickoff=ko.isoformat(), snapshot=d.get("timestamp", snap),
                             bookmakers=(d.get("data") or {}).get("bookmakers", [])))
            time.sleep(0.3)
    json.dump(rows, open(path, "w"))
    nb = sum(len(r["bookmakers"]) for r in rows)
    print(f"{season} wk{week}: {len(rows)} games, {nb} book-markets | spent {spent}, remaining {remaining}", flush=True)
    if spent >= BUDGET or (remaining is not None and remaining < FLOOR):
        print("budget reached"); break
