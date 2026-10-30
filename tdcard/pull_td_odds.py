"""Live anytime-TD prices for upcoming NFL games (all US books). ~1 credit per game. Writes tdcard/odds_<date>.json"""
import json, os, sys
from datetime import datetime, timezone
import requests
KEY = os.environ["ODDS_API_KEY"]; B = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
ABBR = {"Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL","Buffalo Bills":"BUF","Carolina Panthers":"CAR",
 "Chicago Bears":"CHI","Cincinnati Bengals":"CIN","Cleveland Browns":"CLE","Dallas Cowboys":"DAL","Denver Broncos":"DEN","Detroit Lions":"DET",
 "Green Bay Packers":"GB","Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX","Kansas City Chiefs":"KC",
 "Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC","Los Angeles Rams":"LA","Miami Dolphins":"MIA","Minnesota Vikings":"MIN",
 "New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG","New York Jets":"NYJ","Philadelphia Eagles":"PHI",
 "Pittsburgh Steelers":"PIT","San Francisco 49ers":"SF","Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB","Tennessee Titans":"TEN","Washington Commanders":"WAS"}
season, week, maxdays = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 4
evs = requests.get(f"{B}/events", params={"apiKey": KEY}, timeout=30).json()
now = datetime.now(timezone.utc); out = []
for e in evs:
    t = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
    if not (now < t and (t - now).total_seconds() < maxdays * 86400):
        continue
    r = requests.get(f"{B}/events/{e['id']}/odds", params={"apiKey": KEY, "regions": "us", "markets": "player_anytime_td",
                                                          "oddsFormat": "american"}, timeout=30)
    gid = f"{season}_{week:02d}_{ABBR[e['away_team']]}_{ABBR[e['home_team']]}"
    out.append(dict(game_id=gid, event_id=e["id"], kickoff=e["commence_time"], snapshot=now.isoformat(), bookmakers=r.json().get("bookmakers", [])))
    print(gid, len(out[-1]["bookmakers"]), "books | remaining", r.headers.get("x-requests-remaining"))
path = os.environ.get("ODDS_OUT", f"tdcard/odds_{now:%Y%m%dT%H%M}.json"); json.dump(out, open(path, "w")); print("saved", path)
